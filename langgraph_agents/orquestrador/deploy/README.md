# deploy

Container and Kubernetes manifests for the K8s Diagnostic Assistant.

## Structure

```
orquestrador/
  backend/          # Agent/graph code
  router_ui/        # FastAPI + UI
  deploy/
    Dockerfile      # Single image — bundles backend + router_ui
    k8s/
      secret.yaml   # Env vars: LLM URL, MCP URLs, API keys
      deployment.yaml # App deployment with MCP readiness init-container
      service.yaml  # ClusterIP service on port 80 → 8000
```

## Build the image

Run from the `orquestrador/` directory:

```bash
docker build -f deploy/Dockerfile -t k8s-assistant:latest .
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
kubectl port-forward svc/k8s-assistant 8080:80
# open http://localhost:8080
```

Or add an Ingress / change `service.yaml` to `NodePort` / `LoadBalancer`.

## Notes

- The `initContainer` in the Deployment waits for all four MCP services to respond
  before the app starts. This prevents `CrashLoopBackOff` caused by MCPs not being
  ready yet. Adjust the URLs in `secret.yaml` to match your in-cluster service names.
- `replicas: 1` is intentional — each replica would hold its own in-memory
  LangGraph `MemorySaver`. Scale only after switching to a shared checkpointer
  (e.g. `langgraph-checkpoint-postgres`).
- The `startupProbe` gives the app up to 2 minutes (`12 × 10s`) to finish
  `build_graph()`. Increase `failureThreshold` if your LLM or MCPs are slow to connect.
