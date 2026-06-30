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
import types as _types
if 'langchain.callbacks' not in sys.modules:
    try:
        import langchain.callbacks  # noqa
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


_MAX_CONTENT = 4000  # max chars of tool result sent to the UI


def _serialize_content(content) -> str:
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


def _agent_name_from_tool(tool_name: str) -> str:
    """call_kubernetes_agent → kubernetes"""
    return tool_name.removeprefix("call_").removesuffix("_agent")


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
                    data: dict = {"node": node_name}

                    # ── Orchestrator: dispatches tool calls or gives final answer ──
                    if node_name == "orchestrator":
                        messages = state_update.get("messages") or []
                        last_msg = messages[-1] if messages else None

                        if isinstance(last_msg, AIMessage):
                            tool_calls = getattr(last_msg, "tool_calls", None) or []
                            if tool_calls:
                                # Orchestrator is calling one or more agent tools
                                calls = []
                                for tc in tool_calls:
                                    agent = _agent_name_from_tool(tc.get("name", ""))
                                    task  = tc.get("args", {}).get("task", "")
                                    calls.append({"agent": agent, "task": task})
                                data["tool_calls"] = calls
                            else:
                                # No tool calls → final answer
                                content = _serialize_content(last_msg.content)
                                data["final_answer"] = content
                                data["is_final"]     = True

                        call_stat = state_update.get("last_call_stat")
                        if call_stat:
                            data["call_stat"] = call_stat

                    # ── ToolNode: agent tool results ──────────────────────────────
                    elif node_name == "tool_node":
                        messages = state_update.get("messages") or []
                        results  = []
                        for msg in messages:
                            if isinstance(msg, ToolMessage):
                                agent   = _agent_name_from_tool(getattr(msg, "name", "") or "")
                                content = _serialize_content(msg.content)
                                trunc   = len(content) > _MAX_CONTENT
                                results.append({
                                    "agent":     agent,
                                    "content":   content[:_MAX_CONTENT],
                                    "truncated": trunc,
                                })
                        if results:
                            data["agent_results"] = results

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
