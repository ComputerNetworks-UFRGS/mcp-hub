import asyncio
import os
import sys
import types as _types
from functools import partial
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from dotenv import load_dotenv

# ── Compatibility shim ───────────────────────────────────────────────────────
# langfuse 3.x imports BaseCallbackHandler from langchain.callbacks.base,
# which was removed in langchain 1.x (moved to langchain_core.callbacks.base).
# We register a fake module in sys.modules before langfuse loads.
if 'langchain.callbacks' not in sys.modules:
    try:
        import langchain.callbacks  # noqa: already exists (langchain 0.x)
    except (ImportError, ModuleNotFoundError):
        from langchain_core.callbacks import base as _lc_cb_base
        _cb_base_mod = _types.ModuleType('langchain.callbacks.base')
        for _attr in dir(_lc_cb_base):
            setattr(_cb_base_mod, _attr, getattr(_lc_cb_base, _attr))
        _cb_mod = _types.ModuleType('langchain.callbacks')
        _cb_mod.base = _cb_base_mod
        sys.modules.setdefault('langchain', _types.ModuleType('langchain'))
        sys.modules['langchain.callbacks']      = _cb_mod
        sys.modules['langchain.callbacks.base'] = _cb_base_mod
        del _lc_cb_base, _cb_base_mod, _cb_mod, _attr
# ────────────────────────────────────────────────────────────────────────────

from langfuse.langchain import CallbackHandler as LangfuseHandler

from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, RemoveMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from state import GraphState
from orchestrator import MagenticOrchestrator
from agents import make_agent
from reflect import ReflectNode

load_dotenv()


def get_model() -> ChatOpenAI:
    return ChatOpenAI(
        model=str(os.getenv("MODELO_OPEN_WEB_UI", "gpt-oss:20b")),
        base_url=str(os.getenv("OLLAMA_BASE_URL", "http://localhost:1234")),
        temperature=0.0,
        # Ask the server to include token usage in the final streaming chunk.
        # Ollama ≥ 0.5 and any OpenAI-compatible server that supports the
        # stream_options extension will return usage counts; older servers
        # silently ignore this and usage falls back to 0.
        stream_usage=True,
    )


def reset_ledger(state: GraphState) -> dict:
    """
    Resets all investigation state at the start of each new user message.
    Plain fields are overwritten directly; tool-history lists (add_messages)
    require RemoveMessage to be properly cleared.
    """
    update: dict = {
        "task_ledger":       "",
        "progress_ledger":   "",
        "action":            "",
        "next_agent":        "",
        "task_for_agent":    "",
        "last_agent":        "",
        "iteration_count":   0,
        "final_answer":      "",
        "kubernetes_answer": "",
        "traces_answer":     "",
        "logs_answer":       "",
        "metrics_answer":    "",
        "github_answer":    "",
        "last_call_stat":    None,
        # session_ledger is intentionally NOT reset here —
        # it accumulates facts across all questions in this conversation
    }

    # RemoveMessage is the only way to clear add_messages-annotated lists
    for name in ("kubernetes", "traces", "logs", "metrics", "github"):
        key = f"{name}_tool_history"
        history = state.get(key) or []
        if history:
            update[key] = [RemoveMessage(id=m.id) for m in history]

    return update


async def build_graph():
    model = get_model()

    # ── Connect to MCP servers ────────────────────────────────────────────────
    clients = {
        "kubernetes": MultiServerMCPClient({"k8s": {
            "transport": "http",
            "url": os.getenv("MCP_K8S_OPERATIONS_URL", "http://localhost:49447/mcp"),
        }}),  # type: ignore
        "traces": MultiServerMCPClient({"traces": {
            "transport": "http",
            "url": os.getenv("MCP_JAEGER_URL", "http://localhost:58191/mcp"),
        }}),  # type: ignore
        "logs": MultiServerMCPClient({"logs": {
            "transport": "http",
            "url": os.getenv("MCP_K8S_LOGS_URL", "http://localhost:494/mcp"),
        }}),  # type: ignore
        "metrics": MultiServerMCPClient({"metrics": {
            "transport": "sse",
            "url": os.getenv("MCP_PROMETHEUS_URL", "http://localhost:52414/sse"),
        }}),  # type: ignore
        "github": MultiServerMCPClient({"github": {
            "transport": "http",
            "url": os.getenv("MCP_GITHUB_URL", "https://api.githubcopilot.com/mcp"),
            "headers": {
                "Authorization": f'Bearer {os.getenv("MCP_GITHUB_KEY", "github-pat-...")}',
            }
        }}),  # type: ignore
    }

    tools = {name: await client.get_tools() for name, client in clients.items()}

    # Limit GitHub tools to read/investigate operations — the full GitHub MCP
    # exposes ~40 tools, which overflows the local model's function-calling capacity
    # and causes malformed tool calls (e.g. <|channel|> token artifacts).
    _GITHUB_ALLOWED = {
        "get_commit", "get_file_contents", "get_label", "get_latest_release",
        "get_release_by_tag", "get_tag", "issue_read", "issue_write",
        "list_branches", "list_commits", "list_issue_fields", "list_issue_types",
        "list_issues", "list_pull_requests", "list_releases", "list_tags",
        "pull_request_read", "search_code", "search_commits", "search_issues",
        "search_pull_requests", "search_repositories", "update_pull_request",
        "update_pull_request_branch",
    }
    tools["github"] = [t for t in tools["github"] if t.name in _GITHUB_ALLOWED]

    # ── Build graph ───────────────────────────────────────────────────────────
    orchestrator = MagenticOrchestrator(llm=model)
    reflect_node = ReflectNode(llm=model)

    graph = StateGraph(GraphState)
    graph.add_node("reset_ledger", reset_ledger)
    graph.add_node("orchestrator", orchestrator)
    graph.add_node("reflect",      reflect_node)

    for name, agent_tools in tools.items():
        graph.add_node(f"{name}_agent",     make_agent(name, model, agent_tools))
        graph.add_node(f"{name}_tool_node", ToolNode(
            tools=agent_tools,
            messages_key=f"{name}_tool_history",
            handle_tool_errors=True,
        ))

        # tool loop: agent → tool_node → agent (until no tool calls)
        graph.add_conditional_edges(
            f"{name}_agent",
            partial(tools_condition, messages_key=f"{name}_tool_history"),
            {"tools": f"{name}_tool_node", END: "orchestrator"},
        )
        graph.add_edge(f"{name}_tool_node", f"{name}_agent")

    # Entry: always reset before orchestrator
    graph.add_edge(START, "reset_ledger")
    graph.add_edge("reset_ledger", "orchestrator")

    # Orchestrator routes to agents or reflect (on final answer)
    graph.add_conditional_edges(
        "orchestrator",
        MagenticOrchestrator.route,
        {
            "kubernetes_agent": "kubernetes_agent",
            "traces_agent":     "traces_agent",
            "logs_agent":       "logs_agent",
            "metrics_agent":    "metrics_agent",
            "github_agent":    "github_agent",
            "reflect":          "reflect",
        },
    )

    # Reflect always terminates
    graph.add_edge("reflect", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main():
    app = await build_graph()
    thread_id = "magentic-1"
    tz_minus_3 = timezone(timedelta(hours=-3))
    session_id = str(datetime.now(tz_minus_3))
    print("Magentic system ready. Type 'exit' to quit.\n")

    while True:
        prompt = input("You: ").strip()
        if prompt.lower() in ("exit", "quit", "sair"):
            break

        langfuse_handler = LangfuseHandler(
            trace_context={"trace_id": uuid4().hex[:32]},
        )
        config = {
            "callbacks": [langfuse_handler],
            "metadata": {"langfuse_session_id": session_id},
            "configurable": {"thread_id": thread_id},
        }
        print()

        async for chunk in app.astream(
            {"messages": [HumanMessage(content=prompt)]},
            config,
            stream_mode="updates",
        ):
            for node_name, update in chunk.items():

                if node_name == "reset_ledger":
                    continue

                def _s(val) -> str:
                    if isinstance(val, list):
                        return "\n".join(str(x) for x in val)
                    return str(val) if val is not None else ""

                # ── Orchestrator updates ──────────────────────────────────────
                if node_name == "orchestrator":
                    task_ledger = _s(update.get("task_ledger"))
                    if task_ledger:
                        print("╔══ TASK LEDGER " + "═" * 45)
                        for line in task_ledger.strip().splitlines():
                            print(f"║  {line}")
                        print("╚" + "═" * 60 + "\n")

                    progress = _s(update.get("progress_ledger"))
                    if progress:
                        print("┌── PROGRESS LEDGER " + "─" * 41)
                        for line in progress.strip().splitlines():
                            print(f"│  {line}")
                        print("└" + "─" * 60 + "\n")

                    if update.get("action") == "dispatch":
                        agent = update.get("next_agent", "?")
                        task  = _s(update.get("task_for_agent"))
                        print(f"  ▶ Dispatching to [{agent.upper()}]")
                        print(f"    Task: {task}\n")

                    final = _s(update.get("final_answer"))
                    if final:
                        print("\n" + "═" * 60)
                        print("FINAL ANSWER:")
                        print(final)
                        print("═" * 60 + "\n")

                # ── Agent node: show tool calls it requested ───────────────────
                elif node_name.endswith("_agent"):
                    agent_label = node_name.replace("_agent", "").upper()
                    hist_key    = node_name.replace("_agent", "_tool_history")
                    for msg in (update.get(hist_key) or []):
                        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                            for tc in msg.tool_calls:
                                print(f"    [{agent_label}] → {tc.get('name', '')}({tc.get('args', {})})")

                    # Final response from this agent
                    answer_key = node_name.replace("_agent", "_answer")
                    answer = _s(update.get(answer_key))
                    if answer:
                        print(f"\n  ◀ [{agent_label}] responded:")
                        print(f"    {answer.strip()}\n")

                # ── Reflect node: show what was learned ───────────────────────
                elif node_name == "reflect":
                    session = _s(update.get("session_ledger"))
                    if session:
                        print("◈ SESSION LEDGER updated:")
                        for line in session.strip().splitlines():
                            print(f"  {line}")
                        print()

                    from reflect import ENABLE_KNOWLEDGE_BASE
                    if ENABLE_KNOWLEDGE_BASE and update:
                        # knowledge.md was written — just signal it
                        print("◈ knowledge.md updated\n")

                # ── ToolNode: show tool results ────────────────────────────────
                elif node_name.endswith("_tool_node"):
                    agent_label = node_name.replace("_tool_node", "").upper()
                    hist_key    = node_name.replace("_tool_node", "_tool_history")
                    for msg in (update.get(hist_key) or []):
                        if isinstance(msg, ToolMessage):
                            content = msg.content
                            if len(content) > 300:
                                content = content[:300] + "…"
                            print(f"    [{agent_label}] ← {msg.name}: {content}")



async def visualize_workflow():
    app = await build_graph()
    try:
        graph_png = app.get_graph().draw_mermaid_png()
        with open("workflow_graph.png", "wb") as f:
            f.write(graph_png)
    except Exception as e:
        print(f"Could not visualize workflow: {e}")


if __name__ == "__main__":
    # asyncio.run(visualize_workflow())
    asyncio.run(main())