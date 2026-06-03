# deploy

Container and Kubernetes manifests for the Magentic Diagnostic Assistant.

## Structure

```
magentic/
  backend/          # Agent/graph code (also works as standalone CLI)
  magentic_ui/      # FastAPI + UI
  deploy/
    Dockerfile      # Single image — bundles backend + magentic_ui
    k8s/
      secret.yaml   # Env vars: LLM URL, MCP URLs, API keys
      deployment.yaml # App deployment with MCP readiness init-container
      service.yaml  # ClusterIP service on port 80 → 8000
```

## Build the image

Run from the `magentic/` directory:

```bash
docker build -f deploy/Dockerfile -t magentic-assistant:latest .
```

## Deploy to Kubernetes

**1. Fill in `k8s/secret.yaml`** with the actual service names and keys for your cluster.

**2. Apply in order:**

```bash
kubectl apply -f deploy/k8s/secret.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

**3. Access the UI:**

```bash
kubectl port-forward svc/magentic-assistant 8080:80
# open http://localhost:8080
```

Or add an Ingress / change `service.yaml` to `NodePort` / `LoadBalancer`.

## Notes

- The `initContainer` in the Deployment waits for all four MCP services to respond
  before the app starts. This prevents `CrashLoopBackOff` caused by MCPs not being
  ready yet. Adjust the URLs in `secret.yaml` to match your in-cluster service names.
- `replicas: 1` is intentional — each replica holds its own in-memory LangGraph
  `MemorySaver`. Scale only after switching to a shared checkpointer
  (e.g. `langgraph-checkpoint-postgres`).
- The `startupProbe` gives the app up to 2 minutes (`12 × 10s`) to finish
  `build_graph()`. Increase `failureThreshold` if your LLM or MCPs are slow to connect.
- `knowledge.md` lives inside the container and is lost when the pod restarts.
  To persist it across restarts, mount a `PersistentVolumeClaim` at `/app/backend`.
