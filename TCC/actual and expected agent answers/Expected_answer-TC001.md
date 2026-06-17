Root Cause
The slowness affects only orders above R$500. When an order exceeds this threshold, the checkout service synchronously executes an expensive detect_fraud() function that blocks the request thread for 45–60 seconds, consuming 100% CPU during that time. Orders below R$500 skip this code path entirely, completing in under 200ms.

Evidence from Observability
Prometheus:

rate(process_cpu_seconds_total{pod="checkout-*"}[1m]) spikes to 2.0+ precisely when high-value orders arrive.
histogram_quantile(0.95, http_request_duration_seconds_bucket{endpoint="/checkout"}) shows a bimodal distribution: ~100ms for low-value orders and ~45s for high-value orders.

Jaeger:

High-value checkout traces (order.total > 500) show total span duration of 45,000–60,000ms.
These traces contain a child span fraud_detection accounting for nearly all of that duration.
The fraud_detection span carries attributes order.total > 500 and user.id, confirming the trigger condition.
Low-value checkout traces show no fraud_detection span and complete in 100–200ms.

Logs:

No errors are logged; requests complete successfully but with extreme latency, making the issue invisible without tracing.


Why Only Some Orders Are Affected
There is a hard-coded conditional in checkout.py:
pythonif total > 500:
    fraud_risk = detect_fraud(user_id, order_items_payload, total)
Only orders exceeding R$500 enter this branch. The detect_fraud() function performs CPU-bound nested loops, unindexed database queries, and a blocking 60-second time.sleep() (simulating an external API call with no timeout), all synchronously on the HTTP request thread.

Impact
While a high-value order is being processed, the thread is fully blocked. Concurrent checkout requests queue up and eventually time out, causing cascading failures visible to all users on the frontend when multiple high-value orders arrive simultaneously.

Recommended Fix
Move fraud detection off the request thread using asynchronous processing:
python# Immediate response — don't block the request
order = create_pending_order(user_id, items, total)

if total > 500:
    detect_fraud_async.delay(user_id, items, order.id)  # background job

return jsonify(order), 201
              
  # 3. Delay – keep short or remove
    time.sleep(0.1)          # <--- change
             
Additional hardening: add a timeout on the external fraud API call, cache results per user, and add a circuit breaker so a slow fraud service cannot bring down checkout entirely.