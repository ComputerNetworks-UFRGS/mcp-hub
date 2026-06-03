# Cluster Knowledge Base

*Automatically updated by the Magentic agent after each conversation.*
*Feel free to edit, correct, or delete entries manually.*


---
**2026-06-01 16:14**
- Namespace minimal-boutique contains 9 running pods.

---
**2026-06-01 16:16**
- The frontend pod in minimal-boutique runs a Vite preview server on port 5173, exposing local and network URLs.

---
**2026-06-01 16:26**
The metrics agent can provide CPU usage metrics for a pod via Prometheus queries.

---
**2026-06-01 16:41**
- In Kubernetes clusters, `host.docker.internal` is not resolvable; Vite proxy configurations should target internal service names (e.g., `orders-service.minimal-boutique.svc.cluster.local`) or external URLs.
- Vite’s CJS Node API is deprecated; consider migrating to the ESM API or updating Vite to avoid warnings.

---
**2026-06-01 16:58**
- ServiceAccount 'imsilva:k8s-mcp' has limited RBAC: can list nodes and pods in the default namespace but cannot list namespaces or pods in all namespaces.
- Cluster has 31 nodes.

---
**2026-06-01 17:20**
The products service pod has no liveness or readiness probes configured, and its container has only a memory request/limit of 1Gi with no CPU request/limit. These are potential sources of instability.

---
**2026-06-02 12:48**
- Namespace minimal-boutique hosts the payment service.
- Deployment name: payment, pod name: payment-8589fbf775-pz8jc.
- Service name: payment.
- Pod labels: app=payment, tier=backend.
- Payment service traces: 20 error traces in last 24h, each with 3 spans, all error=True, exception log "Erro durante o pagamento".

---
**2026-06-02 12:53**
- The payment service produced 100 error traces in the last 24 hours, each with error:true tag or HTTP 500 status, and the common exception message “Erro durante o pagamento”.
- The error traces cluster around 2026‑06‑02 13:45:05‑07 UTC.
- Inner server span of each trace is in service p2 and contains the exception.
- All error traces are associated with the root span POST /payment/charge.
- No 2xx traces were present in this error set.

---
**2026-06-02 14:19**
* Service account `imsilva:k8s-mcp` can list nodes and pods only in the `default` namespace; it cannot list namespaces or pods across all namespaces.
* The hostname `host.docker.internal` is not resolvable inside the cluster; Vite proxy configurations should target internal service names (e.g., `orders-service.minimal-boutique.svc.cluster.local`).
* Vite’s CJS Node API is deprecated; migrating to the ESM API or updating Vite is recommended to avoid warnings.
* The cluster has 31 nodes.
* The `minimal-boutique` namespace contains services: `payment`, `frontend` (Vite preview on port 5173), `orders-service`, `products`, etc.
* The payment service deployment lacks liveness/readiness probes and only requests 1 Gi of memory.
* Prometheus metrics can be queried via the provided `/metrics` endpoints for various UPF and core network services.
* The metrics agent can provide CPU usage metrics for a pod via Prometheus queries.

---
**2026-06-03 13:18**
- Frontend pod name: frontend-5c964b6f89-kkg9t
- Pod labels: app=frontend, pod-template-hash=5c964b6f89
- Container: frontend