import json
import logging
import os

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

app = FastAPI(title="Agent Studio")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ── Profile endpoints ──────────────────────────────────────────────────────────

@app.get("/api/profiles")
async def list_profiles():
    return prof.list_profiles()


@app.get("/api/profiles/{pid}")
async def get_profile(pid: str):
    try:
        return prof.load_profile(pid)
    except FileNotFoundError:
        raise HTTPException(404, f"Profile '{pid}' not found")


@app.post("/api/profiles")
async def save_profile(profile: dict):
    return prof.save_profile(profile)


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    prof.delete_profile(pid)
    return {"ok": True}


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


# ── Chat endpoint ──────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    thread_id: str
    profile: dict  # full profile object sent by client


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


_MAX_TOOL_CONTENT = 3000  # chars; truncate large tool results


def _content_str(content) -> str:
    """Convert ToolMessage.content to a plain string.

    MCP tools may return a list of content blocks such as
    [{"type": "text", "text": "..."}] instead of a bare string.
    """
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


@app.post("/chat")
async def chat(req: ChatRequest):
    async def stream():
        try:
            graph = await get_or_build(req.profile)
        except Exception as e:
            logger.error("Failed to build graph: %s", e, exc_info=True)
            yield _sse({"error": f"Failed to build graph: {e}"})
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

                # ── Custom events from agent_dispatcher (tool_call mode) ───
                if mode == "custom":
                    yield _sse(payload)
                    continue

                # ── Node update events ─────────────────────────────────────
                for node_name, update in payload.items():
                    if node_name in ("reset", "agent_dispatcher"):
                        continue

                    data: dict = {"node": node_name}

                    stat = update.get("last_call_stat")
                    if stat:
                        data["call_stat"] = stat

                    # ── single agent node ──────────────────────────────────
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

                    # ── single tool node ───────────────────────────────────
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

                    # ── orchestrator node ──────────────────────────────────
                    elif node_name == "orchestrator":
                        action = update.get("action") or ""
                        if action:
                            # JSON dispatch mode (orchestrator / magentic)
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
                            # Tool-call mode: infer action from the AIMessage
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

                    # ── specialist agent node (JSON dispatch modes) ────────
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

                    # ── specialist tool node (JSON dispatch modes) ─────────
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
            yield _sse({"error": str(e)})

        yield _sse({"done": True})

    return StreamingResponse(stream(), media_type="text/event-stream")
