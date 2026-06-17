Root‑cause

The checkout service only takes 30‑60 s and the CPU jumps to 100 % when an order with a total > R$ 500 is processed.
In backend/checkout/routes/checkout.py the function detect_fraud() is executed only for those high‑value orders and it contains:

# 1. regex‑heavy loop (≈ 5 M iterations)
re.search(r"(\d+){1,50}", item_descriptions)

# 2. md5‑heavy loop (≈ 3 M iterations)
hash_val = md5(f"{user_id}{j}".encode()).hexdigest()

# 3. deliberate block
time.sleep(60.0)
The two loops are CPU‑bound and the sleep(60) blocks the request thread for a full minute.
Because the checkout endpoint is a single‑threaded Flask app, one such request ties up the worker process – the process CPU is pegged to 100 % and the request waits for the sleep to finish.
Orders with a total ≤ R$ 500 skip the fraud block, so they finish in < 1 s.

1. What to check first?
Tool        What you get        Why it’s useful
list_jaeger_traces (service=checkout, operation=fraud_detection)        Trace IDs with timestamps        Verify that every slow request actually contains the fraud_detection span
search_jaeger_traces (service=checkout, tags='{"http.status_code":"403"}')        All 403 responses        Correlate blocked‑order responses with the fraud algorithm
get_trace_service_breakdown (trace_id)        CPU‑heavy spans        See that fraud_detection accounts for > 90 % of the total time
2. How to fix the slow orders
Option        What it does        Pros        Cons
A. Remove time.sleep(60)        The function no longer blocks the HTTP thread        Immediate speed‑up; no CPU waste        The test scenario (sleep was probably for demo) is lost
B. Run fraud detection asynchronously        Off‑load to a worker queue (Celery, RQ, or a separate micro‑service)        Keeps HTTP response < 1 s; heavy CPU work isolated        Adds infrastructure & complexity
C. Replace CPU‑heavy loops with efficient code        Use vectorized regex, caching, or a proper fraud‑detection library        Keeps sync flow but faster        Still blocking but far less heavy
D. Scale the checkout pods        Horizontal pod autoscaler with high request queue        Handles more concurrent requests        Won’t reduce per‑request latency; still stuck on the 60 s sleep
Recommended first step – Remove the sleep (or set it to a very short delay, e.g. time.sleep(0.1)).
If you need to keep a delay for demonstration or back‑pressure reasons, move the whole fraud check to a background job.

3. Quick code patch (in-place change)
def detect_fraud(user_id, items):
    fraud_score = 0.0

    # 1. Regex‑heavy loop
    item_descriptions = "|".join([str(item) for item in items])
    for i in range(5_000_000):
        re.search(r"(\d+){1,50}", item_descriptions)
        fraud_score += 0.00001

    # 2. MD5 loop
    for j in range(3_000_000):
        hash_val = md5(f"{user_id}{j}".encode()).hexdigest()
        if hash_val.startswith("0"):
            fraud_score += 0.0001

    # 3. Delay – keep short or remove
    time.sleep(0.1)          # <--- change

    return min(fraud_score + 0.1, 1.0)
Deploy this change to a new branch, open a PR, and run your integration tests.
After deployment, re‑run a few > R$ 500 orders; the response time should drop below 1 s and the CPU will no longer reach 100 %.

4. Verify post‑fix
Run a stress test (e.g., wrk or a simple Python script) with 10–20 high‑value orders in parallel.
Expect CPU to stay ≤ 30 % and response times ≈ 300 ms.
Check Prometheus:
sum(rate(container_cpu_usage_seconds_total{pod=~"checkout-.*"}[1m])) should stay steady.
Review Jaeger: the fraud_detection span should now be ≤ 200 ms.
5. Longer‑term improvements
Item        Action        Why it matters
Code review        Remove unnecessary loops and hard‑coded sleeps        Keeps the service lightweight
Observability        Add a @tracer.start_as_current_span("fraud_check") around the algorithm        Gives fine‑grained metrics
Testing        Add unit tests for detect_fraud() with pytest‑asyncio        Detects accidental re‑introduction of delays
Scalability        Consider a separate fraud‑service or worker queue        Allows scaling independent of checkout throughput
Bottom line:
The slowdown is caused by the intentional time.sleep(60) and CPU‑heavy loops inside detect_fraud().
Removing the sleep (or moving the fraud logic to a background job) will eliminate the 30‑60 s latency and stop the CPU from spiking to 100 %.