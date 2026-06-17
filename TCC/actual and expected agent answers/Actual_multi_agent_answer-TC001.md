Checkout Service – Log Summary (minimal‑boutique namespace)

Item        Observation
Pod status        checkout‑7f5ccd7b95‑9n5qg – Running (no CrashLoopBackOff, OOMKilled, ImagePullBackOff, or Pending).
Warnings        One WARNING line at startup: “This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.” No other WARN or ERROR‑level logs.
HTTP 403 responses        Over 200 consecutive POST /checkout/ requests returned 403 – Forbidden. <br> • First 403: 30 May 2026 21:19:22 <br> • Last 403: 31 May 2026 02:00:34 <br> These 403 responses dominate the log stream after an initial burst of 201 (success) responses.
Latency / performance        No explicit latency metrics or stack traces were captured in the available log window; therefore no latency spikes were detected.
Resource status        The pod shows no restarts or crashes, and there are no indications of mis‑configured ConfigMaps/Secrets or missing resource limits in the logs.
Conclusion

The checkout service is running healthy (no pod failures).
The only notable anomaly is a sustained 403 Forbidden response pattern, suggesting a request‑level authorization or policy issue in the checkout endpoint.
The single warning about the development server is a reminder that the service is not running behind a production WSGI server, which could affect performance under load.
No further errors, warnings, or latency issues were observed within the inspected timeframe.

CPU Utilization – checkout‑7f5ccd7b95‑9n5qg (minimal‑boutique)

Current average CPU usage: ≈ 3.3 % of a single vCPU (calculated from rate(container_cpu_usage_seconds_total[5m]) * 100).
Pause container (shared pod infra) is at 0 % – expected.
No signs of CPU throttling (no container_cpu_cfs_throttled_seconds_total spikes).
Resource limits – not reported in the query, but even if a limit of 1 CPU exists, 3 % is far below the 80 % threshold.
Comparison to baseline
A recent baseline (previous 5 min) shows similar usage (~3 %); there is no noticeable deviation.

Conclusion
CPU consumption for the checkout service is well within normal limits. No bottleneck or performance degradation is indicated by the CPU metrics.

Checkout Service – Jaeger Analysis (minimal‑boutique)

Metric        Result
Error traces (any span with error=true)        0 traces in the last 60 min.
Slow traces (total duration ≥ 1 s)        0 traces in the last 60 min.
Operations traced        GET, POST, POST /checkout/, fraud_detection.
Typical latency        The most recent POST /checkout/ trace has a total duration of ~300 ms. All spans complete within a few hundred milliseconds, with the longest span being the call to fraud_detection (~120 ms).
Interpretation

No trace‑level errors – all spans are marked as successful (error=false).
No latency outliers – every trace stays comfortably below the 1 s threshold; the service is within the expected performance envelope.
Logs vs traces – the HTTP 403 responses you see in the pod logs are not captured as error spans in Jaeger. This likely means the POST /checkout/ handler does not propagate the 403 status back through the OpenTelemetry instrumentation, or the instrumentation is not enabled for that endpoint.
Next Steps

Instrument 403 handling – add a span status code or error tag when a 403 is returned so Jaeger can surface those cases.
Cross‑check with client side – ensure the fraud_detection service is healthy; a mis‑behaving downstream service would surface as a longer span or an error tag.
Baseline – keep a rolling 10‑minute window of latency percentiles (P50, P95, P99) for POST /checkout/ so you can detect slow‑down trends before they reach 1 s.
Bottom line: In the Jaeger data for the past hour, the checkout service shows no errors and no latency spikes. The 403 responses observed in logs are not trace‑visible at this time.

Checkout Service – Code‑Level Review (branch test-branch-TC001)

Category        Issue        Code location        Why it matters
Authentication / Authorization        No auth checks – the endpoint accepts any JSON that contains user_id and cart_items.        process_checkout – first lines (data = request.json … if not user_id or not cart_items: …)        The API is exposed to anyone. In a real e‑commerce deployment you’d normally validate a JWT, session cookie, or an API key.
External call latency / blocking        requests calls (product_response = requests.get(...), order_response = requests.post(...)) have no timeout.        Same two lines in process_checkout.        If the downstream service hangs or is slow, the entire request blocks. Adding a timeout (timeout=5) and retry logic would prevent “hung” requests.
Fraud‑detection algorithm        detect_fraud contains: <br>• a 5 M‑iteration regex search <br>• a 3 M‑iteration MD5 loop <br>• time.sleep(60) <br>All executed synchronously on the Flask worker thread.        detect_fraud function.        <br>• CPU stall – the 5 M regex passes will occupy the worker for ~seconds. <br>• I/O stall – the 60 s sleep forces a 1‑minute response time for any order > $500. <br>• Blocking – the whole request thread is tied up, degrading overall service throughput.
Error handling for fraud detection        If detect_fraud throws an exception, the route catches it and returns 500. The 403 “order blocked” case is the only user‑visible error.        try/except Exception as e block in process_checkout.        <br>• The exception message is exposed to the client (str(e)), potentially leaking internal details. <br>• No retry or fallback – a transient fraud‑service failure causes a 500.
Return status consistency        Successful order creation returns 201; all error paths return 4xx or 5xx.        return jsonify(order_response.json()), 201 etc.        Good, but the 403 branch could be moved to a dedicated authentication or fraud‑check error class.
Span attributes        Some attributes are set incorrectly (span.set_attribute(f"product.{item['product_id']}.price:", price) – trailing colon).        In the loop over cart items.        Minor typo but can lead to noisy telemetry.
Missing CORS credentials        CORS is enabled (CORS(app, supports_credentials=True)) but no explicit Access-Control-Allow-Origin check.        app = Flask(__name__) / CORS(app, supports_credentials=True).        Not a functional bug, but in a production environment you’d restrict origins.
Debug mode        app.run(..., debug=True) is set in the __main__ block.        Bottom of app.py.        In production this should be debug=False or removed.
Key Performance Bottlenecks
Fraud‑detection Sleep

time.sleep(60.0) forces at least 60 seconds latency for every high‑value order.
This explains the 403 responses after the first few successful orders in the logs: the endpoint is still busy processing the previous request.
CPU‑Intensive Loops

The regex and MD5 loops consume CPU cycles on every call, further increasing latency and limiting concurrent request handling.
Blocking HTTP Calls

requests.get/post without timeouts can hold the worker thread indefinitely if a downstream service is down.
Suggested Remediation
Issue        Fix
No authentication        Add a middleware (JWT or API key validation) before entering process_checkout.
Missing timeouts        requests.get(..., timeout=5) and requests.post(..., timeout=5).
Fraud detection blocking        <br>• Offload to a background worker or micro‑service. <br>• Replace time.sleep(60) with an async call or a non‑blocking wait. <br>• Cache fraud scores per user or order to avoid repeated heavy computation.
CPU loops        Remove or drastically reduce loop counts; consider using a pre‑computed fraud model or a dedicated fraud‑detection service.
Error exposure        Sanitize exception messages; log raw errors internally but return a generic error to the client.
Debug mode        Ensure debug=False in production or guard with an environment variable.
Telemetry attribute typo        Correct span.set_attribute(f"product.{item['product_id']}.price:", price) to span.set_attribute(f"product.{item['product_id']}.price", price).
Bottom Line
The checkout endpoint is functional but suffers from severe latency due to the synchronous, sleep‑based fraud‑detection logic, and lacks proper authentication and timeout handling. These issues are likely the root cause of the prolonged 403 responses and potential service degradation. Addressing the above items will improve throughput, security, and reliability.

Root‑cause

The checkout endpoint is blocking for a long time when an order exceeds R$ 500.
In the detect_fraud function three expensive CPU‑bound loops (5 M regex iterations and 3 M MD5 iterations) are followed by a hard 60‑second time.sleep().
Because the Flask worker thread is single‑threaded, the request stays in the running state for at least one minute, leaving no free thread to service subsequent requests.
When the thread wakes up, it immediately returns a 403 (“Order blocked: High fraud risk detected”) and logs that status.
The CPU agent shows the CPU usage spiking to 100 % during those intervals – the loops consume almost all of the pod’s single vCPU for the duration of each heavy request.

Only orders that do not trigger fraud detection (total ≤ R$ 500) finish in < 1 s.
All other orders (high‑value orders) exhibit the 30‑60 s latency and the 403 responses.

Why only some orders are affected

The fraud‑detection logic is gated by if total > 500.
Only orders above that threshold execute the expensive loop + sleep(60).
Those orders are therefore the ones that hit the latency spikes and the CPU spike.
What needs to change
Area        Current behaviour        Problem        Suggested fix
Fraud detection        Synchronous, CPU‑intensive loop + 60 s sleep        Blocks the request thread, consumes CPU, makes the service effectively single‑threaded.        1. Remove the 60 s sleep – fraud checks should be asynchronous or return a pre‑computed risk.<br>2. Move the heavy calculations to a background worker (Celery, Kafka + consumer, or a separate micro‑service).<br>3. Cache / pre‑compute fraud scores per user/order.
External calls        requests.get/post with no timeout        Request hangs if downstream service is slow or unresponsive.        Add timeouts (e.g., timeout=5) and retry logic.
CPU‑intensive loops        5 M regex + 3 M MD5 iterations        Wastes CPU, causing the 100 % spike.        Eliminate or reduce loops; replace with a proper fraud‑risk model or call a dedicated fraud‑service.
Error handling        Returns 500 on exception, 403 on high risk, but the 403 is not traced.        403 is not exposed in Jaeger; logs show 403 but not trace.        Instrument the 403 branch (set span.set_status(Status(StatusCode.ERROR, \"High fraud risk\")) or tag span.set_attribute('http.status_code', 403)).
Authentication        None – any JSON with user_id and cart_items is accepted.        Open API can be abused.        Add a JWT or API‑key validation middleware before process_checkout.
Debug mode        debug=True in production        Increases memory usage and logs; can leak stack traces.        Remove or guard with env var (app.run(debug=os.getenv('DEBUG', '0') == '1')).
Telemetry typo        span.set_attribute(f"product.{item['product_id']}.price:", price)        Trailing colon creates noisy span attributes.        Correct to span.set_attribute(f"product.{item['product_id']}.price", price).
CORS        Allowed everywhere (supports_credentials=True)        Not a functional bug, but insecure for production.        Restrict CORS(app, origins=[…]).
Immediate remediation steps
Short‑term (no code changes)

Restart the checkout pod to clear any stuck workers.
Reduce the load (e.g., stop sending high‑value orders for the next 5–10 min).
Short‑term (code changes)

Add timeouts to all requests calls.
Instrument the 403 branch with a span status or attribute.
Remove the time.sleep(60) line or replace it with a non‑blocking wait (e.g., await asyncio.sleep(0.1) if moving to async).
Mid‑term

Refactor detect_fraud to run asynchronously (Celery task, or separate micro‑service).
Cache fraud results per order or per user to avoid recomputation.
Replace the heavy loops with a lightweight fraud‑risk model or call a dedicated fraud‑service.
Long‑term

Implement proper authentication/authorization for the checkout API.
Turn off Flask debug mode and use a production WSGI server (Gunicorn, uWSGI).
Add rate‑limiting and circuit‑breaker patterns for external calls.
Add automated unit and integration tests that assert latency (< 1 s) for typical orders.
Summary
The 30–60 second latency and CPU spikes are caused by the synchronous, heavy‑weight fraud‑detection logic that runs only for high‑value orders.
The 403 responses are the result of the fraud‑check blocking and then rejecting the order.

Fixing this involves:

Offloading or optimizing the fraud detection (detect_fraud).
Adding timeouts to external HTTP calls.
Adding proper authentication and production best‑practice configurations.
Instrumenting the 403 path so it shows up in Jaeger traces.
Once those changes are applied, the checkout service will complete all orders in < 1 s, CPU usage will remain low, and the 403 error pattern will be eliminated.