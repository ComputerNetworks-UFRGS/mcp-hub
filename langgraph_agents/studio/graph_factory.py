"""
Builds LangGraph apps dynamically from a profile dict.

Supported structures:
  - single       : one LLM + all MCP tools, standard ReAct loop
  - orchestrator : routing LLM dispatches to per-MCP specialist agents (no task ledger)
  - magentic     : same as orchestrator but with task ledger + progress ledger
  - tool_call    : orchestrator dispatches sub-agents via LLM tool calling
"""

import hashlib
import json
import logging
import re
import time
from functools import partial

logger = logging.getLogger(__name__)
from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import StreamWriter
from pydantic import BaseModel, Field

MAX_ITERATIONS = 10
_MAX_TOOL_CONTENT = 3000

# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_usage(response) -> dict:
    if response is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    meta = getattr(response, "usage_metadata", None)
    if meta:
        inp = int(meta.get("input_tokens", 0) or 0)
        out = int(meta.get("output_tokens", 0) or 0)
        return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}
    rm = getattr(response, "response_metadata", {}) or {}
    tu = rm.get("token_usage") or rm.get("usage") or {}
    inp = int(tu.get("prompt_tokens", 0) or 0)
    out = int(tu.get("completion_tokens", 0) or 0)
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": int(tu.get("total_tokens", 0) or inp + out)}


def _parse_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group())
        raise


async def _astream(llm, messages) -> tuple[AIMessage | None, int, int | None]:
    """Stream LLM, return (merged AIMessage | None, duration_ms, ttft_ms | None)."""
    t0 = time.monotonic()
    t_first: float | None = None
    chunks: list[AIMessageChunk] = []
    try:
        async for chunk in llm.astream(messages):
            if t_first is None:
                t_first = time.monotonic()
            chunks.append(chunk)
    except Exception as e:
        logger.error("LLM stream error: %s", e, exc_info=True)
        return None, int((time.monotonic() - t0) * 1000), None
    if not chunks:
        return None, int((time.monotonic() - t0) * 1000), None
    merged = chunks[0]
    for c in chunks[1:]:
        merged = merged + c
    resp = AIMessage(
        content=merged.content,
        tool_calls=list(getattr(merged, "tool_calls", []) or []),
        usage_metadata=getattr(merged, "usage_metadata", None),
        response_metadata=getattr(merged, "response_metadata", {}),
        id=getattr(merged, "id", None),
    )
    dur = int((time.monotonic() - t0) * 1000)
    ttft = int((t_first - t0) * 1000) if t_first is not None else None
    return resp, dur, ttft


def _call_stat(node: str, resp, dur: int, ttft: int | None) -> dict:
    u = _extract_usage(resp)
    return {"node": node, "duration_ms": dur, "ttft_ms": ttft, **u}


def _interpolate_creds(value: str, credentials: dict) -> str:
    """Replace {{key}} placeholders in header values with credential values."""
    def replace(m):
        return credentials.get(m.group(1), m.group(0))
    return re.sub(r'\{\{(\w+)\}\}', replace, value)


async def _load_tools(mcp_cfg: dict, credentials: dict = {}, username: str = "") -> list:
    headers = {}
    for k, v in (mcp_cfg.get("headers") or {}).items():
        headers[k] = _interpolate_creds(str(v), credentials)
    if username:
        headers["X-Remote-User"] = f"{username}-readonly"

    transport = mcp_cfg.get("transport", "http")
    if transport == "http":
        transport = "streamable_http"

    cfg: dict = {
        mcp_cfg["id"]: {
            "transport": transport,
            "url": mcp_cfg["url"],
        }
    }
    if headers:
        cfg[mcp_cfg["id"]]["headers"] = headers

    client = MultiServerMCPClient(cfg)  # type: ignore
    tools = await client.get_tools()
    tool_filter = mcp_cfg.get("tool_filter")
    if tool_filter:
        tools = [t for t in tools if t.name in tool_filter]
    return tools


def _make_llm(profile: dict, credentials: dict = {}) -> ChatOpenAI:
    m = profile.get("model", {})
    raw_key = _interpolate_creds(m.get("api_key", ""), credentials)
    return ChatOpenAI(
        model=_interpolate_creds(m.get("name", "gpt-oss:20b"), credentials),
        base_url=_interpolate_creds(m.get("base_url", "http://localhost:11434/v1"), credentials),
        api_key=raw_key or None,  # None → OpenAI client lê OPENAI_API_KEY do env
        temperature=0.0,
        stream_usage=True,
    )


# ── SINGLE ─────────────────────────────────────────────────────────────────────

async def _build_single(profile: dict, credentials: dict, checkpointer, username: str = "") -> Any:
    llm = _make_llm(profile, credentials)
    enabled = [m for m in profile.get("mcps", []) if m.get("enabled")]
    all_tools: list = []
    for mcp_cfg in enabled:
        all_tools.extend(await _load_tools(mcp_cfg, credentials, username))

    sys_prompt = profile.get("prompts", {}).get(
        "system",
        "You are a helpful assistant. Use the available tools to answer the user's question.",
    )
    sys_msg = SystemMessage(sys_prompt)
    bound = llm.bind_tools(all_tools) if all_tools else llm

    class _State(TypedDict):
        messages: Annotated[list, add_messages]
        last_call_stat: Optional[dict]

    async def agent_node(state: _State) -> dict:
        msgs = [sys_msg] + list(state["messages"])
        resp, dur, ttft = await _astream(bound, msgs)
        if resp is None:
            resp = AIMessage(content="[Agent error: empty response]")
        return {"messages": [resp], "last_call_stat": _call_stat("agent", resp, dur, ttft)}

    g = StateGraph(_State)
    g.add_node("agent", agent_node)
    if all_tools:
        g.add_node("tool_node", ToolNode(all_tools, handle_tool_errors=True))
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", tools_condition, {"tools": "tool_node", END: END})
        g.add_edge("tool_node", "agent")
    else:
        g.add_edge(START, "agent")
        g.add_edge("agent", END)
    return g.compile(checkpointer=checkpointer)


# ── MULTI (orchestrator / magentic) ───────────────────────────────────────────

_DEFAULT_ORCH_SYSTEM = """\
You are the orchestrator of a multi-agent system.
You coordinate specialist agents to answer the user's question.

Available agents:
{agent_list}

Always respond ONLY with valid JSON. No text outside the JSON block."""

_DEFAULT_AGENT_PROMPT = """\
You are a specialist agent with access to {name} tools.
Complete the assigned task and provide a clear summary of your findings.
When you have no more tool calls to make, write your final summary."""

_MAGENTIC_PHASE1 = """\
The user asked:
{question}

Create a task ledger:
1. What needs to be discovered
2. Which agents are relevant and why
3. Suggested investigation order

Then select the first agent and write a specific task for it.

Respond with JSON:
{{
  "task_ledger": "<plan>",
  "next_agent": "<agent_id>",
  "task_for_agent": "<instruction>"
}}"""

_MAGENTIC_PHASE2 = """\
Task Ledger:
{task_ledger}

Progress Ledger:
{progress_ledger}

The '{last_agent}' agent just responded:
{last_agent_answer}

Update the progress ledger. Is the task complete?

If complete:
{{
  "action": "final_answer",
  "progress_ledger": "<updated>",
  "final_answer": "<answer for the user>"
}}

If incomplete:
{{
  "action": "dispatch",
  "progress_ledger": "<updated>",
  "next_agent": "<agent_id>",
  "task_for_agent": "<instruction>"
}}"""

_ROUTER_PHASE1 = """\
The user asked:
{question}

Available agents: {agents}

Which agent should handle this first, and what should it do?

Respond with JSON:
{{"next_agent": "<agent_id>", "task_for_agent": "<specific task>"}}"""

_ROUTER_PHASE2 = """\
Progress so far:
{progress}

The '{last_agent}' agent responded:
{last_agent_answer}

Is the task complete, or should another agent investigate?

If complete:
{{"action": "final_answer", "final_answer": "<answer>"}}

If incomplete:
{{"action": "dispatch", "next_agent": "<agent_id>", "task_for_agent": "<task>"}}"""


class _SafeDict(dict):
    """Returns '{key}' for missing keys so partial format strings don't crash."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


async def _build_multi(profile: dict, mode: str, credentials: dict, checkpointer, username: str = "") -> Any:
    llm = _make_llm(profile, credentials)
    enabled = [m for m in profile.get("mcps", []) if m.get("enabled")]
    agent_ids = [m["id"] for m in enabled]
    prompts = profile.get("prompts", {})

    if mode == "magentic":
        phase1_tpl = prompts.get("magentic_phase1") or _MAGENTIC_PHASE1
        phase2_tpl = prompts.get("magentic_phase2") or _MAGENTIC_PHASE2
    else:
        phase1_tpl = prompts.get("orchestrator_phase1") or _ROUTER_PHASE1
        phase2_tpl = prompts.get("orchestrator_phase2") or _ROUTER_PHASE2

    tools_map: dict[str, list] = {}
    for mcp_cfg in enabled:
        tools_map[mcp_cfg["id"]] = await _load_tools(mcp_cfg, credentials, username)

    fields: dict[str, Any] = {
        "messages":        Annotated[list, add_messages],
        "action":          str,
        "next_agent":      str,
        "task_for_agent":  str,
        "last_agent":      str,
        "final_answer":    str,
        "iteration_count": int,
        "task_ledger":     str,
        "progress_ledger": str,
        "last_call_stat":  Optional[dict],
    }
    for aid in agent_ids:
        fields[f"{aid}_tool_history"] = Annotated[list, add_messages]
        fields[f"{aid}_answer"]       = str
    DynState = TypedDict("DynState", fields)  # type: ignore

    agent_list_str = "\n".join(
        f"  - {m['id']} : {m.get('name', m['id'])}" for m in enabled
    )
    raw_orch_sys = prompts.get("orchestrator", "") or _DEFAULT_ORCH_SYSTEM
    orch_sys_msg = SystemMessage(raw_orch_sys.replace("{agent_list}", agent_list_str))

    async def orchestrator_node(state: DynState) -> dict:  # type: ignore
        iteration = (state.get("iteration_count") or 0) + 1
        task_ledger = state.get("task_ledger") or ""

        if iteration > MAX_ITERATIONS:
            progress = state.get("progress_ledger") or ""
            final = f"Reached iteration limit ({MAX_ITERATIONS}).\n\nProgress:\n{progress}"
            return {
                "action": "final_answer", "final_answer": final, "iteration_count": iteration,
                "messages": [AIMessage(content=final)],
            }

        is_first = not task_ledger
        question = (state.get("messages") or [{}])[-1].content if state.get("messages") else ""
        last = state.get("last_agent") or ""
        last_ans = state.get(f"{last}_answer") or "(no response)" if last else ""

        if is_first:
            user_prompt = phase1_tpl.format_map(_SafeDict(
                question=question,
                agents=", ".join(agent_ids),
            ))
        else:
            user_prompt = phase2_tpl.format_map(_SafeDict(
                task_ledger=task_ledger,
                progress_ledger=state.get("progress_ledger") or "(empty)",
                progress=state.get("progress_ledger") or "(empty)",
                last_agent=last,
                last_agent_answer=last_ans,
            ))

        resp, dur, ttft = await _astream(llm, [orch_sys_msg, HumanMessage(user_prompt)])
        if resp is None:
            final = "Orchestrator failed to respond."
            return {
                "action": "final_answer", "final_answer": final, "iteration_count": iteration,
                "messages": [AIMessage(content=final)],
            }

        try:
            data = _parse_json(resp.content)
        except Exception as e:
            final = f"Orchestrator parse error: {e}\n\nRaw:\n{resp.content}"
            return {
                "action": "final_answer", "final_answer": final, "iteration_count": iteration,
                "messages": [AIMessage(content=final)],
            }

        update: dict = {
            "iteration_count": iteration,
            "last_call_stat": _call_stat("orchestrator", resp, dur, ttft),
        }

        if is_first and mode == "magentic":
            update.update({
                "task_ledger":    str(data.get("task_ledger", "")),
                "progress_ledger": "",
                "action":         "dispatch",
                "next_agent":     str(data.get("next_agent", agent_ids[0] if agent_ids else "")),
                "task_for_agent": str(data.get("task_for_agent", "")),
            })
        elif is_first:
            update.update({
                "action":         "dispatch",
                "next_agent":     str(data.get("next_agent", agent_ids[0] if agent_ids else "")),
                "task_for_agent": str(data.get("task_for_agent", "")),
            })
        else:
            action = data.get("action", "final_answer")
            update["action"] = action
            if action == "dispatch":
                update["next_agent"]     = str(data.get("next_agent", ""))
                update["task_for_agent"] = str(data.get("task_for_agent", ""))
                if mode == "magentic":
                    update["progress_ledger"] = str(
                        data.get("progress_ledger") or state.get("progress_ledger") or ""
                    )
                else:
                    prev = state.get("progress_ledger") or ""
                    update["progress_ledger"] = (prev + f"\n[{last}]: {last_ans}").strip() if last else prev
            else:
                final = str(data.get("final_answer", ""))
                update["final_answer"] = final
                update["messages"] = [AIMessage(content=final)]
                if "progress_ledger" in data:
                    update["progress_ledger"] = str(data["progress_ledger"])

        return update

    def reset_node(state: DynState) -> dict:  # type: ignore
        out: dict = {
            "action": "", "next_agent": "", "task_for_agent": "", "last_agent": "",
            "final_answer": "", "iteration_count": 0, "task_ledger": "",
            "progress_ledger": "", "last_call_stat": None,
        }
        for aid in agent_ids:
            out[f"{aid}_answer"] = ""
            history = state.get(f"{aid}_tool_history") or []
            if history:
                out[f"{aid}_tool_history"] = [RemoveMessage(id=m.id) for m in history]
        return out

    def orch_route(state: DynState) -> str:  # type: ignore
        if (state.get("action") or "") == "final_answer":
            return END
        nxt = state.get("next_agent") or ""
        return f"{nxt}_agent" if nxt in agent_ids else END

    sub_agent_stateful = profile.get("sub_agent_memory", "stateful") != "stateless"

    def make_agent_node(aid: str, mcp_cfg: dict):
        tools = tools_map[aid]
        bound = llm.bind_tools(tools) if tools else llm
        raw = prompts.get(aid, "") or _DEFAULT_AGENT_PROMPT.format(name=mcp_cfg.get("name", aid))
        sys_msg = SystemMessage(raw)
        hist_key = f"{aid}_tool_history"
        ans_key  = f"{aid}_answer"

        async def node(state: DynState) -> dict:  # type: ignore
            history = list(state.get(hist_key) or [])
            task = state.get("task_for_agent") or "Investigate and report findings."
            is_tool_continuation = bool(history) and isinstance(history[-1], ToolMessage)

            if is_tool_continuation:
                msgs = [sys_msg] + history
                extra = []
            elif sub_agent_stateful:
                human_msg = HumanMessage(task)
                msgs = [sys_msg] + history + [human_msg]
                extra = [human_msg]
            else:
                removes = [RemoveMessage(id=m.id) for m in history]
                human_msg = HumanMessage(task)
                msgs = [sys_msg, human_msg]
                extra = removes + [human_msg]

            resp, dur, ttft = await _astream(bound, msgs)
            if resp is None:
                resp = AIMessage(content="[Agent error: empty response]")
            out: dict = {
                hist_key: extra + [resp],
                "last_call_stat": _call_stat(f"{aid}_agent", resp, dur, ttft),
            }
            if not getattr(resp, "tool_calls", None):
                out[ans_key] = resp.content
                out["last_agent"] = aid
            return out

        node.__name__ = f"{aid}_agent"
        return node

    g = StateGraph(DynState)
    g.add_node("reset",        reset_node)
    g.add_node("orchestrator", orchestrator_node)

    for mcp_cfg in enabled:
        aid = mcp_cfg["id"]
        tools = tools_map[aid]
        g.add_node(f"{aid}_agent",     make_agent_node(aid, mcp_cfg))
        g.add_node(f"{aid}_tool_node", ToolNode(
            tools=tools,
            messages_key=f"{aid}_tool_history",
            handle_tool_errors=True,
        ))
        g.add_conditional_edges(
            f"{aid}_agent",
            partial(tools_condition, messages_key=f"{aid}_tool_history"),
            {"tools": f"{aid}_tool_node", END: "orchestrator"},
        )
        g.add_edge(f"{aid}_tool_node", f"{aid}_agent")

    g.add_edge(START, "reset")
    g.add_edge("reset", "orchestrator")
    route_map = {END: END, **{f"{aid}_agent": f"{aid}_agent" for aid in agent_ids}}
    g.add_conditional_edges("orchestrator", orch_route, route_map)

    return g.compile(checkpointer=checkpointer)


# ── TOOL CALL ──────────────────────────────────────────────────────────────────

_DEFAULT_TC_ORCH_SYSTEM = """\
You are the orchestrator of a multi-agent system.
You coordinate specialist agents to answer the user's question.

Available agents (callable as tools):
{agent_list}

Use the available tools to delegate tasks to specialist agents.
When you have gathered enough information, provide a comprehensive final answer."""


async def _build_tool_call(profile: dict, credentials: dict, checkpointer, username: str = "") -> Any:
    llm = _make_llm(profile, credentials)
    enabled = [m for m in profile.get("mcps", []) if m.get("enabled")]
    agent_ids = [m["id"] for m in enabled]
    prompts = profile.get("prompts", {})
    sub_agent_stateful = profile.get("sub_agent_memory", "stateful") != "stateless"

    tools_map: dict[str, list] = {}
    for mcp_cfg in enabled:
        tools_map[mcp_cfg["id"]] = await _load_tools(mcp_cfg, credentials, username)

    fields: dict[str, Any] = {
        "messages":       Annotated[list, add_messages],
        "last_call_stat": Optional[dict],
    }
    for aid in agent_ids:
        fields[f"{aid}_tool_history"] = Annotated[list, add_messages]
        fields[f"{aid}_answer"]       = str
    DynState = TypedDict("DynState", fields)  # type: ignore

    agent_list_str = "\n".join(
        f"  - {m['id']} : {m.get('name', m['id'])}" for m in enabled
    )
    raw_orch_sys = prompts.get("orchestrator", "") or _DEFAULT_TC_ORCH_SYSTEM
    orch_sys_msg = SystemMessage(raw_orch_sys.replace("{agent_list}", agent_list_str))

    class _AgentInput(BaseModel):
        task: str = Field(description="The specific task for this specialist agent.")

    agent_stubs = [
        StructuredTool.from_function(
            func=lambda task: "",
            name=m["id"],
            description=f"Delegate a task to the {m.get('name', m['id'])} specialist agent.",
            args_schema=_AgentInput,
        )
        for m in enabled
    ]
    orch_bound = llm.bind_tools(agent_stubs) if agent_stubs else llm

    async def orchestrator_node(state: DynState) -> dict:  # type: ignore
        msgs = [orch_sys_msg] + list(state.get("messages") or [])
        resp, dur, ttft = await _astream(orch_bound, msgs)
        if resp is None:
            resp = AIMessage(content="[Orchestrator error: empty response]")
        return {
            "messages": [resp],
            "last_call_stat": _call_stat("orchestrator", resp, dur, ttft),
        }

    def orch_route(state: DynState) -> str:  # type: ignore
        msgs = state.get("messages") or []
        if not msgs:
            return END
        last = msgs[-1]
        return "agent_dispatcher" if getattr(last, "tool_calls", None) else END

    async def agent_dispatcher(state: DynState, writer: StreamWriter) -> dict:  # type: ignore
        msgs = state.get("messages") or []
        tool_calls = getattr(msgs[-1], "tool_calls", []) if msgs else []
        out: dict = {"messages": []}
        last_stat = None

        for tc in tool_calls:
            aid = tc["name"]
            task = tc["args"].get("task", "")
            if aid not in agent_ids:
                out["messages"].append(ToolMessage(
                    content=f"Unknown agent: {aid}",
                    tool_call_id=tc["id"], name=aid,
                ))
                continue

            mcp_cfg = next((m for m in enabled if m["id"] == aid), {})
            sub_tools = tools_map[aid]
            sub_bound = llm.bind_tools(sub_tools) if sub_tools else llm
            raw = prompts.get(aid, "") or _DEFAULT_AGENT_PROMPT.format(
                name=mcp_cfg.get("name", aid))
            sys_msg = SystemMessage(raw)
            hist_key = f"{aid}_tool_history"
            existing = list(state.get(hist_key) or [])

            if sub_agent_stateful and existing:
                human_msg = HumanMessage(task)
                loop_msgs = [sys_msg] + existing + [human_msg]
                history_update: list = [human_msg]
            else:
                removes = [RemoveMessage(id=m.id) for m in existing]
                human_msg = HumanMessage(task)
                loop_msgs = [sys_msg, human_msg]
                history_update = removes + [human_msg]

            while True:
                resp, dur, ttft = await _astream(sub_bound, loop_msgs)
                if resp is None:
                    resp = AIMessage(content="[Agent error: empty response]")
                last_stat = _call_stat(f"{aid}_agent", resp, dur, ttft)
                loop_msgs.append(resp)
                history_update.append(resp)

                if not resp.tool_calls:
                    writer({"node": f"{aid}_agent", "agent_id": aid,
                            "answer": resp.content, "call_stat": last_stat})
                    out[hist_key] = history_update
                    out[f"{aid}_answer"] = resp.content
                    break

                writer({"node": f"{aid}_agent", "agent_id": aid,
                        "tool_calls": [{"name": t["name"], "args": t["args"]}
                                       for t in resp.tool_calls],
                        "call_stat": last_stat})

                tool_results_evt = []
                for sub_tc in resp.tool_calls:
                    tool_fn = next((t for t in sub_tools if t.name == sub_tc["name"]), None)
                    try:
                        full = str(await tool_fn.ainvoke(sub_tc["args"])) if tool_fn else f"Tool '{sub_tc['name']}' not found"
                    except Exception as e:
                        full = f"Tool error: {e}"
                    chars = len(full)
                    content = full[:_MAX_TOOL_CONTENT] + "…" if chars > _MAX_TOOL_CONTENT else full
                    tool_msg = ToolMessage(content=content, tool_call_id=sub_tc["id"], name=sub_tc["name"])
                    loop_msgs.append(tool_msg)
                    history_update.append(tool_msg)
                    tool_results_evt.append({"name": sub_tc["name"], "content": content, "chars": chars})

                writer({"node": f"{aid}_tool_node", "agent_id": aid,
                        "tool_results": tool_results_evt})

            out["messages"].append(ToolMessage(
                content=out.get(f"{aid}_answer", ""),
                tool_call_id=tc["id"], name=aid,
            ))

        if last_stat:
            out["last_call_stat"] = last_stat
        return out

    def reset_node(state: DynState) -> dict:  # type: ignore
        out: dict = {}
        for aid in agent_ids:
            out[f"{aid}_answer"] = ""
            history = state.get(f"{aid}_tool_history") or []
            if history:
                out[f"{aid}_tool_history"] = [RemoveMessage(id=m.id) for m in history]
        return out

    g = StateGraph(DynState)
    g.add_node("reset",            reset_node)
    g.add_node("orchestrator",     orchestrator_node)
    g.add_node("agent_dispatcher", agent_dispatcher)
    g.add_edge(START, "reset")
    g.add_edge("reset", "orchestrator")
    g.add_conditional_edges("orchestrator", orch_route, {
        "agent_dispatcher": "agent_dispatcher",
        END: END,
    })
    g.add_edge("agent_dispatcher", "orchestrator")
    return g.compile(checkpointer=checkpointer)


# ── Public cache ───────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {}


def _profile_hash(profile: dict, credentials: dict = {}, username: str = "") -> str:
    relevant = {k: profile.get(k) for k in ("agent_structure", "sub_agent_memory", "model", "mcps", "prompts")}
    profile_h = hashlib.sha256(
        json.dumps(relevant, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    cred_h = hashlib.sha256(
        json.dumps({"c": credentials, "u": username}, sort_keys=True, default=str).encode()
    ).hexdigest()[:8]
    return f"{profile_h}_{cred_h}"


async def get_or_build(profile: dict, credentials: dict = {}, checkpointer=None, username: str = "") -> Any:
    key = _profile_hash(profile, credentials, username)
    if key not in _cache:
        cp = checkpointer or MemorySaver()
        structure = profile.get("agent_structure", "single")
        if structure == "single":
            _cache[key] = await _build_single(profile, credentials, cp, username)
        elif structure == "tool_call":
            _cache[key] = await _build_tool_call(profile, credentials, cp, username)
        else:
            _cache[key] = await _build_multi(profile, structure, credentials, cp, username)
    return _cache[key]
