import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend', '.env'))

# ── Compatibility shim ───────────────────────────────────────────────────────
# langfuse 3.x imports BaseCallbackHandler from langchain.callbacks.base,
# which was removed in langchain 1.x (moved to langchain_core.callbacks.base).
# We register a fake module in sys.modules before langfuse loads, so the import
# resolves to the langchain-core equivalent. Must run before `from main import`.
import types as _types
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

from main import build_graph
from langfuse.langchain import CallbackHandler as LangfuseHandler
from uuid import uuid4

graph_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global graph_app
    print("Building graph...")
    graph_app = await build_graph()
    print("Graph ready.")
    yield

app = FastAPI(lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


class ChatRequest(BaseModel):
    message: str
    thread_id: str


# Nodes that are internal plumbing — hide from the UI trace
_HIDDEN_NODES = {"add_final_agent_message"}

# Maps each agent/tool node to the state-key prefix it uses
_TOOL_PREFIX: dict[str, str] = {
    "kubernetes_agent":     "kubernetes",
    "traces_agent":         "traces",
    "logs_agent":           "logs",
    "metrics_agent":        "metrics",
    "kubernetes_tool_node": "kubernetes",
    "traces_tool_node":     "traces",
    "logs_tool_node":       "logs",
    "metrics_tool_node":    "metrics",
}

_MAX_CONTENT = 4000  # max chars sent per tool result


def _serialize_content(content) -> str:
    """Turn any LangChain message content into a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", json.dumps(item, ensure_ascii=False)))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


@app.post("/chat")
async def chat(request: ChatRequest):
    async def generate():
        try:
            langfuse_handler = LangfuseHandler(
                trace_context={"trace_id": uuid4().hex[:32], "session_id": request.thread_id},
            )
            async for chunk in graph_app.astream(
                {"messages": [HumanMessage(content=request.message)]},
                {
                    "callbacks": [langfuse_handler],
                    "configurable": {"thread_id": request.thread_id},
                },
                stream_mode="updates",
            ):
                for node_name, state_update in chunk.items():
                    if node_name in _HIDDEN_NODES:
                        continue

                    data: dict = {"node": node_name}

                    if node_name == "orchestrator":
                        action       = state_update.get("action", "")
                        target_agent = state_update.get("target_agent", "") or ""
                        message      = state_update.get("message_to_agent", "") or ""
                        thought      = state_update.get("thought", "") or ""

                        data["action"]       = action
                        data["target_agent"] = target_agent
                        data["thought"]      = thought
                        data["message"]      = message

                        if action in ("final_resolution", "ask_user"):
                            data["final_answer"] = message
                            data["is_final"]     = True

                        call_stat = state_update.get("last_call_stat")
                        if call_stat:
                            data["call_stat"] = call_stat

                    elif node_name in _TOOL_PREFIX:
                        prefix   = _TOOL_PREFIX[node_name]
                        hist     = state_update.get(f"{prefix}_tool_history", [])

                        if node_name.endswith("_tool_node"):
                            # ToolMessages — results returned by tools
                            results = []
                            for msg in hist:
                                if isinstance(msg, ToolMessage):
                                    raw   = _serialize_content(msg.content)
                                    trunc = len(raw) > _MAX_CONTENT
                                    results.append({
                                        "name":      getattr(msg, "name", "tool"),
                                        "content":   raw[:_MAX_CONTENT],
                                        "truncated": trunc,
                                    })
                            if results:
                                data["tool_results"] = results
                        else:
                            # AIMessage with tool_calls — agent requesting a tool
                            calls = []
                            for msg in hist:
                                if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                                    for tc in msg.tool_calls:
                                        try:
                                            args = json.loads(json.dumps(tc.get("args", {}), ensure_ascii=False))
                                        except (TypeError, ValueError):
                                            args = str(tc.get("args", {}))
                                        calls.append({
                                            "name": tc.get("name", ""),
                                            "args": args,
                                        })
                            if calls:
                                data["tool_calls"] = calls

                            # Agent final response (AIMessage with no tool_calls)
                            answer_msg = state_update.get(f"{prefix}_answer")
                            if answer_msg and isinstance(answer_msg, AIMessage):
                                if not getattr(answer_msg, "tool_calls", None):
                                    content = _serialize_content(answer_msg.content)
                                    if content:
                                        trunc = len(content) > _MAX_CONTENT
                                        data["agent_response"]           = content[:_MAX_CONTENT]
                                        data["agent_response_truncated"] = trunc

                            # Emit call_stat for every agent LLM call (tool loops included)
                            call_stat = state_update.get("last_call_stat")
                            if call_stat:
                                data["call_stat"] = call_stat

                    yield f"data: {json.dumps(data)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield 'data: {"done": true}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
