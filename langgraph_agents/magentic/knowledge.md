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