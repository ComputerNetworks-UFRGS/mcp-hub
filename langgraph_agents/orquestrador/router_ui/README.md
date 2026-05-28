# router_ui

Chat interface for the `orquestrador` multi-agent system.  
Sends messages to the LangGraph orchestrator and displays, in real time, which agents were called, what tools they invoked, and the final diagnosis.

## Prerequisites

- Python 3.11+
- `orquestrador/.env` configured (see `orquestrador/.env_example`)
- All MCP servers running and reachable at the URLs defined in `.env`
- LLM accessible at `OLLAMA_BASE_URL`

## Setup

```bash
cd langgraph_agents/router_ui
pip install fastapi "uvicorn[standard]"
# All other dependencies come from orquestrador/requirements.txt
```

## Running

```bash
uvicorn app:app --reload
```

Open **http://localhost:8000** in your browser.

## What you'll see

Each assistant response shows three layers:

| Layer | Description |
|---|---|
| **Trace badges** | Pipeline of nodes that ran: `Orchestrator · Kubernetes → Kubernetes → K8s Tool → ...` |
| **Tool dropdowns** | Collapsible `call` / `result` items for every tool invocation |
| **Answer bubble** | Final diagnosis (or clarifying question) from the Orchestrator |

Hovering over an **Orchestrator** badge shows its internal reasoning (`thought`).

## Project structure

```
router_ui/
  app.py          # FastAPI backend — imports build_graph from ../orquestrador
  static/
    index.html    # Chat UI (vanilla HTML/CSS/JS, no build step)
  requirements.txt
  README.md
```

The `orquestrador/` source is never modified — `app.py` just adds it to `sys.path` and calls `build_graph()`.
