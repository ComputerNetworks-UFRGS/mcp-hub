import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

import profiles as prof
from graph_factory import get_or_build

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
POSTGRES_URI = os.getenv("POSTGRES_URI", "")

_checkpointer = None
_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _checkpointer, _pool
    if POSTGRES_URI:
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        _pool = AsyncConnectionPool(
            conninfo=POSTGRES_URI,
            max_size=20,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await _pool.open()
        _checkpointer = AsyncPostgresSaver(_pool)
        await _checkpointer.setup()
        prof.set_pool(_pool)
        await prof.setup()
        logger.info("PostgresSaver + profiles table initialized")
    else:
        from langgraph.checkpoint.memory import MemorySaver
        _checkpointer = MemorySaver()
        logger.info("MemorySaver initialized (set POSTGRES_URI for persistence)")
    yield
    if _pool:
        await _pool.close()


app = FastAPI(title="Agent Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ── Profile endpoints ──────────────────────────────────────────────────────────

@app.get("/api/profiles")
async def list_profiles():
    return await prof.list_profiles()


@app.get("/api/profiles/{pid}")
async def get_profile(pid: str):
    try:
        return await prof.load_profile(pid)
    except FileNotFoundError:
        raise HTTPException(404, f"Profile '{pid}' not found")


@app.post("/api/profiles")
async def save_profile(profile: dict):
    return await prof.save_profile(profile)


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    await prof.delete_profile(pid)
    return {"ok": True}


# ── Thread management ──────────────────────────────────────────────────────────

@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str):
    """Delete all LangGraph checkpoints for a thread (conversation history)."""
    if _pool:
        async with _pool.connection() as conn:
            await conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
            await conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
            await conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
    # MemorySaver: ephemeral, nothing to delete persistently
    return {"ok": True}


# ── Tools endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/tools")
async def list_mcp_tools(data: dict):
    try:
        from graph_factory import _load_tools
        tools = await _load_tools(data["mcp"])
        return {"tools": [t.name for t in tools]}
    except Exception as e:
        logger.error("list_mcp_tools error: %s", e, exc_info=True)
        raise HTTPException(500, str(e))


@app.get("/api/defaults")
async def get_defaults():
    """Return model defaults from environment variables."""
    return {
        "model": {
            "name":     os.getenv("MODELO_OPEN_WEB_UI", ""),
            "base_url": os.getenv("OLLAMA_BASE_URL", ""),
            "api_key":  os.getenv("OPENAI_API_KEY", ""),
        }
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "checkpointer": "postgres" if _pool else "memory",
    }


# ── Chat endpoint ──────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    thread_id: str
    profile: dict
    credentials: dict = {}


def _unwrap_exception(e: BaseException) -> str:
    """Unwrap ExceptionGroup / TaskGroup to expose the real root cause."""
    if isinstance(e, BaseExceptionGroup):
        parts = [_unwrap_exception(sub) for sub in e.exceptions]
        return " | ".join(parts)
    cause = e.__cause__ or e.__context__
    if cause and not isinstance(cause, BaseExceptionGroup):
        return _unwrap_exception(cause)
    return f"{type(e).__name__}: {e}"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


_MAX_TOOL_CONTENT = 3000


def _content_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


async def _stream_graph(req: ChatRequest):
    try:
        graph = await get_or_build(req.profile, req.credentials, _checkpointer)
    except Exception as e:
        logger.error("Failed to build graph: %s", e, exc_info=True)
        yield _sse({"error": f"Failed to build graph: {_unwrap_exception(e)}"})
        yield _sse({"done": True})
        return

    config = {"configurable": {"thread_id": req.thread_id}}

    try:
        async for chunk in graph.astream(
            {"messages": [HumanMessage(req.message)]},
            config,
            stream_mode=["updates", "custom"],
        ):
            mode, payload = chunk

            if mode == "custom":
                yield _sse(payload)
                continue

            for node_name, update in payload.items():
                if node_name in ("reset", "agent_dispatcher"):
                    continue

                data: dict = {"node": node_name}
                stat = update.get("last_call_stat")
                if stat:
                    data["call_stat"] = stat

                if node_name == "agent":
                    for msg in (update.get("messages") or []):
                        if isinstance(msg, AIMessage):
                            if msg.content:
                                data["content"] = msg.content
                            if getattr(msg, "tool_calls", None):
                                data["tool_calls"] = [
                                    {"name": tc["name"], "args": tc["args"]}
                                    for tc in msg.tool_calls
                                ]
                    yield _sse(data)

                elif node_name == "tool_node":
                    results = []
                    for msg in (update.get("messages") or []):
                        if isinstance(msg, ToolMessage):
                            full = _content_str(msg.content)
                            c = full[:_MAX_TOOL_CONTENT] + "…" if len(full) > _MAX_TOOL_CONTENT else full
                            results.append({"name": msg.name, "content": c, "chars": len(full)})
                    if results:
                        data["tool_results"] = results
                    yield _sse(data)

                elif node_name == "orchestrator":
                    action = update.get("action") or ""
                    if action:
                        data["action"] = action
                        for key in ("task_ledger", "progress_ledger"):
                            val = update.get(key)
                            if val:
                                data[key] = val
                        if action == "dispatch":
                            data["next_agent"]     = update.get("next_agent", "")
                            data["task_for_agent"] = update.get("task_for_agent", "")
                        elif action == "final_answer":
                            data["final_answer"] = update.get("final_answer", "")
                        yield _sse(data)
                    else:
                        for msg in (update.get("messages") or []):
                            if isinstance(msg, AIMessage):
                                if getattr(msg, "tool_calls", None):
                                    data["action"] = "dispatch"
                                    data["tool_calls"] = [
                                        {"name": tc["name"], "args": tc["args"]}
                                        for tc in msg.tool_calls
                                    ]
                                elif msg.content:
                                    data["action"] = "final_answer"
                                    data["final_answer"] = msg.content
                        if data.get("action"):
                            yield _sse(data)

                elif node_name.endswith("_agent"):
                    aid = node_name[:-6]
                    data["agent_id"] = aid
                    for msg in (update.get(f"{aid}_tool_history") or []):
                        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                            data.setdefault("tool_calls", []).extend([
                                {"name": tc["name"], "args": tc["args"]}
                                for tc in msg.tool_calls
                            ])
                    answer = update.get(f"{aid}_answer")
                    if answer:
                        data["answer"] = answer
                    yield _sse(data)

                elif node_name.endswith("_tool_node"):
                    aid = node_name[:-10]
                    data["agent_id"] = aid
                    results = []
                    for msg in (update.get(f"{aid}_tool_history") or []):
                        if isinstance(msg, ToolMessage):
                            full = _content_str(msg.content)
                            c = full[:_MAX_TOOL_CONTENT] + "…" if len(full) > _MAX_TOOL_CONTENT else full
                            results.append({"name": msg.name, "content": c, "chars": len(full)})
                    if results:
                        data["tool_results"] = results
                    yield _sse(data)

    except Exception as e:
        logger.error("Chat stream error: %s", e, exc_info=True)
        yield _sse({"error": _unwrap_exception(e)})

    yield _sse({"done": True})


@app.post("/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(_stream_graph(req), media_type="text/event-stream")


# ── Stateless run endpoint (for watcher / external triggers) ───────────────────

class RunRequest(BaseModel):
    prompt: str
    profile_id: str
    thread_id: str = ""
    credentials: dict = {}


@app.post("/api/run")
async def run(req: RunRequest):
    """Run a prompt against a saved profile. Returns when the agent is done.
    Useful for notification triggers (watcher, n8n, etc.)."""
    try:
        profile = await prof.load_profile(req.profile_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Profile '{req.profile_id}' not found")

    thread_id = req.thread_id or f"run-{os.urandom(4).hex()}"

    try:
        graph = await get_or_build(profile, req.credentials, _checkpointer)
    except Exception as e:
        raise HTTPException(500, f"Failed to build graph: {e}")

    config = {"configurable": {"thread_id": thread_id}}
    try:
        result = await graph.ainvoke({"messages": [HumanMessage(req.prompt)]}, config)
        messages = result.get("messages") or []
        final = next(
            (m.content for m in reversed(messages)
             if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None)),
            "",
        )
        if not final:
            final = result.get("final_answer", "")
        return {"response": final, "thread_id": thread_id}
    except Exception as e:
        logger.error("Run error: %s", e, exc_info=True)
        raise HTTPException(500, str(e))
