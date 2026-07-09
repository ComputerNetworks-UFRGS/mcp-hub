# Agent Studio — System Documentation

## Overview

Agent Studio is a browser-based IDE for building and chatting with LangGraph agents backed by MCP (Model Context Protocol) tools. It runs as a FastAPI server and can be deployed per-user namespace in Kubernetes.

---

## Architecture

```
Browser (index.html)
  │  SSE stream / JSON
  ▼
FastAPI (app.py)   ──────────────── PostgresSaver (Postgres)
  │                                      or MemorySaver (dev)
  ▼
graph_factory.py   ── builds LangGraph ── MultiServerMCPClient
                                               │
                                    HTTP headers (Bearer token)
                                               │
                                         k8s-mcp server
                                         (kubectl + SA)
```

**Conversation persistence** is per `thread_id`. With `POSTGRES_URI` set the history survives restarts; without it, `MemorySaver` keeps it in-process only.

---

## Key Components

### `app.py` — FastAPI server

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serves `index.html` |
| `/chat` | POST (SSE) | Streaming chat; returns LangGraph step events |
| `/api/profiles` | GET / POST | List / save profiles |
| `/api/profiles/{id}` | GET / DELETE | Load / delete a profile |
| `/api/tools` | POST | List tools from a given MCP config |
| `/api/defaults` | GET | Return `.env` model defaults |
| `/api/health` | GET | Returns checkpointer type |
| `/api/threads/{id}` | DELETE | Delete checkpoint rows for a thread |
| `/api/run` | POST | Stateless run — used by the watcher |

### `graph_factory.py` — Graph builder & cache

- **`get_or_build(profile, credentials, checkpointer)`** — returns a compiled LangGraph. Result is cached by `sha256(profile_fields)[:16]_sha256(credentials)[:8]`.
- **`_interpolate_creds(value, credentials)`** — replaces `{{key}}` placeholders in MCP header values with actual credential values.
- Agent structures: `single`, `orchestrator`, `magentic`, `tool_call`.

### `profiles.py` — Profile storage

When `POSTGRES_URI` is set, profiles are stored in a `profiles` table (JSONB) in the same Postgres instance used for conversation checkpoints. The table is created automatically on startup (`CREATE TABLE IF NOT EXISTS`).

Without `POSTGRES_URI` (local dev), falls back to JSON files under `PROFILES_DIR` (default: `studio/profiles/`). Override with the `PROFILES_DIR` env var.

### `static/index.html` — Frontend

Built with Alpine.js (no build step). State lives in-browser:
- **Conversations** — `localStorage` key `agent_studio_convs`. Each stores messages, stats, and a profile snapshot.
- **Credentials** — `localStorage` key `agent_studio_creds`. Key-value pairs sent as `credentials: {}` in every `/chat` request.

---

## Credential Injection

MCP HTTP headers support `{{name}}` placeholders:

```
Header:  Authorization: Bearer {{k8s_token}}
Creds:   k8s_token = ey...
Result:  Authorization: Bearer ey...  (only in the HTTP header, never in the LLM context)
```

On the k8s-mcp side a `BearerTokenMiddleware` extracts the token per-request (via `ContextVar`) and prepends `--token <value>` to every `kubectl` call. If no token is sent the pod's service account is used automatically.

---

## Deployment

### Prerequisites

Postgres runs as a **sidecar** in the same pod (defined in `studio-k8s/studio-deployment.yaml`). Create the credentials secret:

```bash
kubectl create secret generic k8s-agent-postgres-credentials \
  --from-literal=POSTGRES_USER=studio \
  --from-literal=POSTGRES_PASSWORD=<password> \
  --from-literal=POSTGRES_DB=agentdb \
  -n YOUR_NAMESPACE
```

The Postgres PVC (`k8s-agent-postgres-data`) must exist — apply it once:

```bash
kubectl apply -f studio-k8s/postgres-pvc.yaml
```

### RBAC for k8s-mcp

Creates the ServiceAccount and gives it read-only (`view`) access for a namespace:

```bash
kubectl apply -f studio-k8s/k8s-mcp-rbac.yaml
```

### Plain YAML

```bash
kubectl apply -f studio-k8s/studio-deployment.yaml
kubectl apply -f studio-k8s/studio-service.yaml
kubectl apply -f studio-k8s/k8s-mcp-deployment.yaml
kubectl apply -f studio-k8s/k8s-mcp-service.yaml
```


### Access

```bash
kubectl port-forward svc/agent-studio 8000:8000 -n YOUR_NAMESPACE
# Open http://localhost:8000
```

---

## Building Images

```bash
# Agent Studio
cd langgraph_agents/studio
docker build -t YOUR_REGISTRY/agent-studio:latest .
docker push YOUR_REGISTRY/agent-studio:latest

# Event Watcher
cd watcher
docker build -t YOUR_REGISTRY/event-watcher:latest .
docker push YOUR_REGISTRY/event-watcher:latest

# k8s-mcp (already on DockerHub as igormsilva/k8s-mcp:latest)
cd k8s-mcp
docker build -t YOUR_REGISTRY/k8s-mcp:latest .
docker push YOUR_REGISTRY/k8s-mcp:latest
```

---

## Event Watcher

Deployed in the `mcp-hub` namespace. Watches Kubernetes `Warning` events cluster-wide (or per specified namespaces), calls `/api/run` on the Agent Studio with a diagnosis prompt, and forwards the agent response to Telegram.

**Required secrets** — edit `watcher/watcher-deployment.yaml`:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `STUDIO_URL` — `http://agent-studio.NAMESPACE:8000`
- `STUDIO_PROFILE_ID` — profile configured in Agent Studio for k8s analysis

Deploy:
```bash
kubectl apply -f watcher/watcher-deployment.yaml
```

---

## Profile Reference

A profile is a JSON file with these fields:

| Field | Description |
|-------|-------------|
| `id` | Auto-generated 8-char hex |
| `name` | Display name |
| `agent_structure` | `single` / `orchestrator` / `magentic` / `tool_call` |
| `sub_agent_memory` | `stateful` / `stateless` (multi-agent modes only) |
| `model.name` | LLM model identifier |
| `model.base_url` | OpenAI-compatible API base URL |
| `model.api_key` | API key (optional) |
| `mcps[]` | List of MCP server configs |
| `mcps[].id` | Unique ID used in routing |
| `mcps[].url` | HTTP URL of the MCP server |
| `mcps[].transport` | `http` (streamable) or `sse` |
| `mcps[].headers` | Dict of headers; values may contain `{{cred_name}}` |
| `mcps[].tool_filter` | List of allowed tool names (empty = all) |
| `prompts.*` | System / orchestrator / phase prompts |
