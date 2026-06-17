Root Cause
The products service is configured with severely restrictive CPU and memory limits in its Kubernetes deployment manifest. These limits are far below what the service needs for normal operation, causing constant CPU throttling, memory exhaustion, and repeated pod crashes.

Evidence from Observability
Kubernetes pod inspection:

kubectl describe pod on the products pod shows OOMKilled events and CrashLoopBackOff status, confirming the pod is being killed repeatedly when memory exceeds its limit.
The deployment manifest reveals the misconfiguration directly:

yamlresources:
  requests:
    memory: "32Mi"
    cpu: "10m"
  limits:
    memory: "64Mi"
    cpu: "50m"
Prometheus:

container_cpu_usage_seconds_total for the products pod is pinned at the 50m limit — 100% throttled at all times.
container_memory_usage_bytes consistently reaches or exceeds the 64Mi limit, triggering OOM kills.
http_requests_total{status="503"} spikes correlate directly with CPU throttling periods and pod restarts.
Request latency degrades from ~50ms baseline to 5,000–30,000ms as memory pressure forces aggressive garbage collection.

Jaeger:

Individual spans for simple product queries show durations of 5–30 seconds, consistent with GC pauses caused by memory pressure rather than any application logic issue.


Why the Service Crashes and Is Slow
With only 64Mi of memory available, the Python runtime cannot buffer normal request traffic. When memory is exhausted, the kernel OOM killer terminates the pod. Between crashes, the 50m CPU limit causes severe throttling — the pod receives so little CPU time that even lightweight requests queue up and time out. The cycle of crash → restart → exhaust memory → crash repeats every few minutes.

Recommended Fix
Increase resource limits to values appropriate for normal operation:
bashkubectl set resources deployment/products \
  -n minimal-boutique \
  --requests=cpu=100m,memory=256Mi \
  --limits=cpu=200m,memory=512Mi
After applying, verify the pod stabilizes:
bashkubectl rollout status deployment/products -n minimal-boutique
kubectl describe pod -l app=products -n minimal-boutique
Latency should return to under 200ms, pod restarts should cease, and CPU throttling should disappear from Prometheus metrics.