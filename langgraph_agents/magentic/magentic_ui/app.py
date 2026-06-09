import sys
import os
import types as _types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
from uuid import uuid4
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend', '.env'))

# ── Compatibility shim ────────────────────────────────────────────────────────
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
# ─────────────────────────────────────────────────────────────────────────────

from langfuse.langchain import CallbackHandler as LangfuseHandler
from main import build_graph

graph_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graph_app
    print("Building Magentic graph…")
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


_MAX_CONTENT = 0   # 0 = no truncation; set to e.g. 20000 to limit very large outputs


def _s(val) -> str:
    """Coerce any value (including lists returned by LLMs) to a string."""
    if isinstance(val, list):
        return "\n".join(str(x) for x in val)
    return str(val) if val is not None else ""


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
                trace_context={
                    "trace_id":  uuid4().hex[:32],
                    "session_id": request.thread_id,
                },
            )
            config = {
                "callbacks":  [langfuse_handler],
                "configurable": {"thread_id": request.thread_id},
            }

            async for chunk in graph_app.astream(
                {"messages": [HumanMessage(content=request.message)]},
                config,
                stream_mode="updates",
            ):
                for node_name, update in chunk.items():
                    # Internal plumbing — nothing useful to send to the UI
                    if node_name == "reset_ledger":
                        continue

                    data: dict = {"node": node_name}

                    # ── LLM call stats (token counts + duration) ──────────────
                    stat = update.get("last_call_stat")
                    if stat:
                        data["call_stat"] = stat

                    # ── Orchestrator ──────────────────────────────────────────
                    if node_name == "orchestrator":
                        task_ledger = _s(update.get("task_ledger") or "")
                        progress    = _s(update.get("progress_ledger") or "")
                        action      = update.get("action", "")

                        if task_ledger:
                            data["task_ledger"] = task_ledger
                        if progress:
                            data["progress_ledger"] = progress
                        data["action"] = action

                        if action == "dispatch":
                            data["next_agent"]     = update.get("next_agent", "")
                            data["task_for_agent"] = _s(update.get("task_for_agent") or "")
                        elif action == "final_answer":
                            final = _s(update.get("final_answer") or "")
                            data["final_answer"] = final
                            data["is_final"]     = True

                    # ── Specialist agents ─────────────────────────────────────
                    elif node_name.endswith("_agent"):
                        prefix   = node_name.replace("_agent", "")
                        hist_key = f"{prefix}_tool_history"
                        hist     = update.get(hist_key) or []

                        calls = []
                        for msg in hist:
                            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                                for tc in msg.tool_calls:
                                    try:
                                        args = json.loads(
                                            json.dumps(tc.get("args", {}), ensure_ascii=False)
                                        )
                                    except (TypeError, ValueError):
                                        args = str(tc.get("args", {}))
                                    calls.append({"name": tc.get("name", ""), "args": args})

                        if calls:
                            data["tool_calls"] = calls

                        answer = update.get(f"{prefix}_answer") or ""
                        if answer:
                            raw   = _s(answer)
                            trunc = _MAX_CONTENT > 0 and len(raw) > _MAX_CONTENT
                            data["agent_response"]           = raw[:_MAX_CONTENT] if trunc else raw
                            data["agent_response_truncated"] = trunc

                    # ── Tool nodes ────────────────────────────────────────────
                    elif node_name.endswith("_tool_node"):
                        prefix   = node_name.replace("_tool_node", "")
                        hist_key = f"{prefix}_tool_history"
                        hist     = update.get(hist_key) or []

                        results = []
                        for msg in hist:
                            if isinstance(msg, ToolMessage):
                                raw   = _serialize_content(msg.content)
                                trunc = _MAX_CONTENT > 0 and len(raw) > _MAX_CONTENT
                                results.append({
                                    "name":      getattr(msg, "name", "tool"),
                                    "content":   raw[:_MAX_CONTENT] if trunc else raw,
                                    "truncated": trunc,
                                })
                        if results:
                            data["tool_results"] = results

                    # ── Reflect ───────────────────────────────────────────────
                    elif node_name == "reflect":
                        session = _s(update.get("session_ledger") or "")
                        if session:
                            data["session_ledger"] = session

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
