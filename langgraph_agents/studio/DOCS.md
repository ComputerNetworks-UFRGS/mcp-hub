# Agent Studio — System Documentation

## Overview

Agent Studio is a browser-based IDE for building and chatting with LangGraph agents backed by MCP (Model Context Protocol) tools. It runs as a FastAPI server and can be deployed per-user namespace in Kubernetes.

---

## Architecture

```
Browser
  │
  ▼
oauth2-proxy  ── Keycloak OIDC ──► login / token validation
  │  (injects X-Forwarded-User header; strips any client-supplied value)
  ▼
FastAPI (app.py)   ──────────────── PostgresSaver (Postgres sidecar)
  │  reads X-Forwarded-User → username                or MemorySaver (dev)
  │  prefixes thread_id: "<username>:<thread_id>"
  │  isolates profiles by owner column
  ▼
graph_factory.py   ── builds LangGraph ── MultiServerMCPClient
                                               │
                                    HTTP header: X-Remote-User: <username>-readonly
                                               │
                                         k8s-mcp server
                                         (_ImpersonateMiddleware reads header)
                                               │
                                         kubectl --as <username>-readonly
                                               │
                                    k8s RBAC enforces read-only view
                                    in the namespaces bound to that virtual user
```

**User isolation** is enforced at three levels:
1. **oauth2-proxy** — only authenticated Keycloak users reach the app; username is injected server-side (cannot be spoofed).
2. **Agent Studio** — profiles and thread_ids are scoped per username.
3. **k8s RBAC** — `<username>-readonly` virtual users have only `view` access in the namespaces explicitly bound to them; the k8s-mcp SA's impersonation permission does not grant any extra read access.

**Conversation persistence** is per `thread_id` (stored as `<username>:<thread_id>` in Postgres). With `POSTGRES_URI` set the history survives restarts; without it, `MemorySaver` keeps it in-process only.

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
| `/api/run` | POST | Stateless run |

### `graph_factory.py` — Graph builder & cache

- **`get_or_build(profile, credentials, checkpointer, username)`** — returns a compiled LangGraph. Result is cached by `sha256(profile_fields + username)[:16]_sha256(credentials + username)[:8]`.
- **`_load_tools(mcp_cfg, credentials, username)`** — resolves credential placeholders and injects `X-Remote-User: <username>-readonly` header before calling the MCP server.
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

Creates the ServiceAccount, grants it read-only view access to its own namespace, and grants it impersonation rights for virtual `*-readonly` users:

```bash
kubectl apply -f studio-k8s/k8s-mcp-rbac.yaml
```

### Per-user provisioning

For each new user, three steps are required:

**1. Allow the k8s-mcp SA to impersonate this user** (add to the ClusterRole):
```bash
kubectl patch clusterrole k8s-mcp-impersonator --type=json \
  -p='[{"op":"add","path":"/rules/0/resourceNames/-","value":"<USERNAME>-readonly"}]'
```

**2. Grant view access in the user's namespace:**
```bash
kubectl create rolebinding <USERNAME>-readonly \
  --clusterrole=view \
  --user=<USERNAME>-readonly \
  -n <NAMESPACE>
```

**3. In Keycloak:** create the user and add them to the `k8s-agent-users` group.

`<USERNAME>` must match the user's Keycloak `preferred_username`. The `-readonly` suffix is appended by Agent Studio automatically. Username editing must be disabled in Keycloak realm settings so the name is stable.

### oauth2-proxy (public URL deployment)

Deploy oauth2-proxy in front of Agent Studio to handle Keycloak OIDC login:

```bash
# Edit oauth2-proxy.yaml first: set Keycloak issuer URL, redirect URL, and fill in the secrets
kubectl apply -f studio-k8s/oauth2-proxy.yaml
```

Then configure your ingress/load balancer to point at `oauth2-proxy:4180` instead of `agent-studio:8000`.

### Plain YAML (Agent Studio + k8s-mcp)

```bash
kubectl apply -f studio-k8s/studio-deployment.yaml
kubectl apply -f studio-k8s/studio-service.yaml
kubectl apply -f studio-k8s/k8s-mcp-deployment.yaml
kubectl apply -f studio-k8s/k8s-mcp-service.yaml
```

### Access (local dev, no oauth2-proxy)

```bash
kubectl port-forward svc/agent-studio 8000:8000 -n YOUR_NAMESPACE
# Open http://localhost:8000
# Without oauth2-proxy, X-Forwarded-User is empty — all data is stored under owner=""
```

---

## Building Images

```bash
# Agent Studio
cd langgraph_agents/studio
docker build -t YOUR_REGISTRY/agent-studio:latest .
docker push YOUR_REGISTRY/agent-studio:latest

# k8s-mcp (already on DockerHub as igormsilva/k8s-mcp:latest)
cd k8s-mcp
docker build -t YOUR_REGISTRY/k8s-mcp:latest .
docker push YOUR_REGISTRY/k8s-mcp:latest
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
