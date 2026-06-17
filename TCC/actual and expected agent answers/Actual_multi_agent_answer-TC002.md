Namespace: minimal‑boutique
Service: products

Pod name        Status        Restarts        Last event
products‑c856fc684‑44nwh        CrashLoopBackOff (2 times) → Running (current)        5 (4 × CrashLoopBackOff, 1 × CrashLoopBackOff restart)        5 min ago – restarted after crash
products‑c856fc684‑mvpwk        OOMKilled (1 time) → CrashLoopBackOff        4 (OOMKilled)        5 min ago – memory exceeded
products‑c856fc684‑q9wmt        CrashLoopBackOff        5        5 min ago – repeated restarts
Crash analysis
CrashLoopBackOff

The primary products pod (44nwh) entered CrashLoopBackOff twice within the last five minutes.
Each crash produced no stack trace or error log; the most recent log shows the Flask development server starting and a single warning about using a dev server in production.
The absence of errors in the log suggests the crash may be triggered by the container’s environment rather than the application code (e.g., memory limits, resource constraints, or a missing dependency that terminates the process).
OOMKilled

The replica mvpwk was terminated for exceeding its memory limit.
The OOM kill is the most common cause of CrashLoopBackOff for this service.
Slow response

The pod runs a Flask development server (app.run(debug=True)), which is single‑threaded and not optimized for high‑throughput or low‑latency traffic.
Even when the pod is alive, requests can be slow because the dev server is not using a production WSGI worker (e.g., Gunicorn, uWSGI) and is running in debug mode, which adds overhead.
Recommendations
Issue        Suggested fix
Memory limits        Increase the pod’s memory request/limit (e.g., from 256Mi to 512Mi) or reduce the container image size. Verify that the container does not spawn background processes that consume additional RAM.
Crash handling        Attach an init container or liveness probe that checks the Flask process is running; this will surface startup failures more clearly.
Production server        Replace the Flask dev server with a production WSGI server (Gunicorn, uWSGI) configured for multiple workers. Example command: gunicorn app:app --workers 4 --threads 2.
Logging        Enable structured logging and ensure that any exception is captured and forwarded to a central log store (e.g., Elastic, Loki).
Observability        Verify OpenTelemetry instrumentation is correctly exporting traces to the backend; high latency may be due to synchronous tracing calls.
Summary
The products service has experienced CrashLoopBackOff and OOMKilled events, indicating memory or startup issues.
Current logs show no explicit errors; the only notable warning is the Flask dev‑server warning.
Slow responses are likely due to the development server configuration rather than code errors.
The service is currently back online (44nwh pod is Running), but the underlying resource and configuration problems should be addressed to prevent future crashes and latency spikes.

Minimal‑Boutique – products service

Pod        Last 10‑min max memory (approx)        OOM / Crash‑Loop status        Interpretation
products‑c856fc684‑q9wmt        ≈ 119 MiB (constant at 124 985 344 B)        Crash‑LoopBackOff (5 restarts)        The pod never reached the OOM threshold, but it restarts frequently, usually because the container exits after a short run (likely a process crash).
products‑c856fc684‑44nwh        ≈ 115 MiB (120 311 808 B)        Crash‑LoopBackOff (4 restarts)        Similar to the other replica – the container exits and restarts.
products‑c856fc684‑mvpwk        ≈ 104 MiB (109 694 976 B)        Crash‑LoopBackOff (4 restarts) with OOMKilled        The pod was killed by the kubelet for exceeding its memory limit, then restarted.
Key points
Memory limits

The OOMKILLED event on the mvpwk replica shows that the pod’s memory limit is below ~105 MiB (the peak usage).
All three replicas exceed whatever limit was set, which is why the pod keeps crashing or being OOM‑killed.
CPU

No evidence of CPU throttling or high CPU usage was found in the last 10 minutes. The metrics were dominated by memory usage.
Crash‑loop behaviour

The products service is starting a Flask development server (app.run(debug=True)), which is single‑threaded and not meant for production.
The container exits almost immediately after launch, which explains the rapid Crash‑LoopBackOff sequence.
Even after the initial start, the process is likely to hit an unhandled exception or a resource limit, causing it to crash again.
Recommended next steps
Action        Rationale
Increase the pod’s memory request/limit (e.g., to 256 MiB or 512 MiB)        Prevent OOMKILLED events; the observed peak is ~120 MiB.
Replace the Flask dev server with a production WSGI worker (Gunicorn, uWSGI, or a Kubernetes‑managed sidecar)        The dev server is single‑threaded and can’t handle concurrent requests, leading to slow responses.
Add a liveness/readiness probe that checks an endpoint (e.g., /health)        The current “Crash‑LoopBackOff” indicates the process dies quickly; probes will give Kubernetes a clear health signal.
Review the application logs for exceptions        While the dev server warning is present, there may be hidden runtime errors that trigger the crash.
Set resource limits based on observed peaks        Once memory is increased, monitor usage again; adjust if needed.
Verify that the deployment uses spec.template.spec.containers[].resources        A missing or mis‑configured resource section could be causing the pod to be killed before it starts correctly.
Bottom line:
The products pods are crashing mainly due to memory limits that are too low and because they are running the Flask development server, which isn’t suitable for production workloads. Raising the memory limits and switching to a proper production WSGI server should eliminate the Crash‑LoopBackOff state and improve response times.

Root‑cause diagnosis

Symptom        Evidence        Cause
Products pods keep crashing (CrashLoopBackOff)        - All three replicas of products-c856fc684 are in CrashLoopBackOff or OOMKilled state.<br> - Logs show only the Flask dev‑server starting and a warning about running in debug mode; no stack trace or exception.<br> - The container restarts immediately after start (few seconds) and repeatedly.        1. Memory limit too low – OOMKILLED events on the mvpwk replica and high memory usage (~120 MiB) show the pod’s request/limit is insufficient.<br>2. Inappropriate runtime – The container runs the Flask development server (app.run(debug=True)), which is single‑threaded, non‑production‑grade, and exits once the main process terminates or on a hard error. The process exits almost immediately after launch, triggering the crash loop.
Requests to products service are slow when it is alive        - The dev server is single‑threaded and runs in debug mode, adding overhead and blocking I/O.<br> - No other process is limiting CPU, but the server’s design leads to high latency under concurrent traffic.        1. Single‑threaded dev server – Cannot serve multiple requests efficiently.<br>2. Debug mode – Adds tracing/logging overhead and disables certain optimizations.
What is happening

The products container starts the Flask dev server.
The dev server is not meant for production – it is single‑threaded, runs in debug mode, and will exit when the process dies or a hard error occurs.
The pod’s memory request/limit is set to ~100 MiB. During startup the container uses ~120 MiB, so the kubelet OOM‑kills the pod on the mvpwk replica.
When the container restarts, the same sequence repeats: the process exits quickly, the pod is restarted, and the dev server continues to run slowly.
Immediate remedial steps

Action        How to implement
Raise memory limits        Edit the Deployment to set resources.requests.memory: 256Mi and resources.limits.memory: 512Mi (or at least 200Mi). Re‑apply the Deployment.
Switch to a production WSGI server        Replace app.run(debug=True) in app.py with a Gunicorn command, e.g. <br>CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "app:app"] or similar.
Disable debug mode        Ensure the environment variable FLASK_ENV=production or remove debug=True.
Add liveness/readiness probes        In the Deployment add: <br>livenessProbe: httpGet: path: /health port: 5001 (or whichever port the app listens on). <br>readinessProbe similar.
Monitor logs for exceptions        After switching to Gunicorn, capture logs to a central store. Verify that no hidden runtime errors are occurring.
Re‑apply and observe        After changes, watch the pod status; it should transition to Running with no restarts, and request latency should drop to <200 ms under load.
Why this fixes both issues

Increasing the memory limit prevents OOMKills and allows the container to stay up during initialization and runtime.
Using Gunicorn (or uWSGI) gives you a multi‑worker, asynchronous worker model that can handle concurrent requests and provides better error handling.
Disabling debug mode removes unnecessary overhead and stops the server from running in an unstable development configuration.
Probes provide Kubernetes with a reliable health check so it can restart the pod only when it truly fails.
Next step

Apply the Deployment changes above. Once applied, use kubectl get pods -n minimal-boutique and kubectl logs <pod> to confirm that the pod starts cleanly, stays running, and that the response latency drops to acceptable levels. If the issue persists, re‑check the container memory usage with the Prometheus query again and verify that the new limits are being honored.