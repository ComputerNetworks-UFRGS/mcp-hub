Root Cause
After the recent backend deployment, the gateway has hardcoded incorrect service names for two internal microservices. PRODUCTS_API_URL points to http://prod456:5001/products/ instead of http://products:5001/products/, and CART_API_URL points to http://carxyz:5005/cart/ instead of http://cart:5005/cart/. Both names fail DNS resolution inside the cluster, causing every request to /products and /cart to fail immediately.

Evidence from Observability
Backend pod logs:

Repeated requests.exceptions.ConnectionError: HTTPConnectionPool(host='prod', port=5001): Max retries exceeded and equivalent errors for carxyz, confirming DNS resolution is failing for both service names.

Jaeger:

Gateway spans for /products and /cart routes have status ERROR.
Span attributes show http.url set to http://prod456:5001/products/ and http://carxyz:5005/cart/, making the misconfigured URLs directly visible.
No downstream spans exist from products or cart services — the connection never reaches them.

Prometheus:

requests_total{endpoint="/products/"} and requests_total{endpoint="/cart/"} show 100% error rate (status 503) starting from the moment of the backend rollout.
All other endpoints are unaffected, isolating the fault to these two gateway routes.

Kubernetes service check:
bashkubectl get svc -n minimal-boutique
This confirms the actual service names are products and cart, neither of which matches the URLs hardcoded in the deployed gateway.

Why Only These Endpoints Are Broken
The backend gateway in backend/routes/gateway.py hardcodes service URLs. The recently deployed image (minimal-boutique-backend:tc003) introduced typos in two constants — prod instead of products and carxyz instead of cart. Kubernetes DNS cannot resolve these names, so every proxied request to those services fails with a connection error before any application logic runs.

Recommended Fix
Correct the service URL constants in gateway.py:
pythonPRODUCTS_API_URL = "http://products:5001/products/"
CART_API_URL = "http://cart:5005/cart/"
Rebuild and redeploy the backend, or roll back to the previous image immediately:
bashkubectl rollout undo deployment/backend -n minimal-boutique
kubectl rollout status deployment/backend -n minimal-boutique
After the rollout, both /products and /cart endpoints should return to normal with no DNS errors in logs and error rate dropping to zero in Prometheus.