Root Cause

The 503 responses you are seeing on /products and /cart are coming from the gateway service.
In the gateway’s gateway.py the upstream URLs are hard-coded to:

ORDERS_API_URL     = "http://orders:5002/orders/"
PRODUCTS_API_URL   = "http://prod456:5001/products/"   # wrong
CHECKOUT_API_URL   = "http://checkout:5003/checkout/"
PAYMENT_API_URL    = "http://payment:5004/payment/"
CART_API_URL       = "http://carxyz:5005/cart/"        # wrong

prod456 and carxyz are not the DNS names of the services in the cluster – the actual services are products and cart.
Because the gateway is trying to hit services that do not exist, it receives connection errors, which the gateway propagates back as HTTP 503.

The recent backend deployment likely replaced the gateway image or its config without updating these URLs, which is why the problem appeared only after the new deploy.

---

Quick Fix

1. Patch the gateway deployment
kubectl -n minimal-boutique edit deployment gateway

Change the hard-coded URLs to the correct service names:

ORDERS_API_URL     = "http://orders:5002/orders/"

* PRODUCTS_API_URL   = "http://prod456:5001/products/"

* PRODUCTS_API_URL   = "http://products:5001/products/"
CHECKOUT_API_URL   = "http://checkout:5003/checkout/"
PAYMENT_API_URL    = "http://payment:5004/payment/"

* CART_API_URL       = "http://carxyz:5005/cart/"

* CART_API_URL       = "http://cart:5005/cart/"

Save and exit. Kubernetes will automatically roll out a new pod.

2. Verify the change
kubectl -n minimal-boutique get pods -l app=gateway -o wide
Make sure the new pod is running.
3. Test the endpoints

* Front-end -> /products -> should return a 200 with product list.
* Front-end -> /cart -> should return a 200 with cart contents.
You can hit the gateway directly with curl to double-check:
curl -s http://:5173/products

---

Optional – Make it Configurable

To avoid future hard-coding mistakes, move the URLs to environment variables:

# In the gateway deployment

env:

* name: ORDERS_URL
value: "http://orders:5002/orders/"
* name: PRODUCTS_URL
value: "http://products:5001/products/"
* name: CART_URL
value: "http://cart:5005/cart/"
* name: CHECKOUT_URL
value: "http://checkout:5003/checkout/"
* name: PAYMENT_URL
value: "http://payment:5004/payment/"

Then in gateway.py:

ORDERS_API_URL   = os.getenv('ORDERS_URL', 'http://orders:5002/orders/')
PRODUCTS_API_URL = os.getenv('PRODUCTS_URL', 'http://products:5001/products/')
CART_API_URL     = os.getenv('CART_URL', 'http://cart:5005/cart/')
CHECKOUT_API_URL = os.getenv('CHECKOUT_URL', 'http://checkout:5003/checkout/')
PAYMENT_API_URL  = os.getenv('PAYMENT_URL', 'http://payment:5004/payment/')

This way any future deployments can set the correct service names without editing code.

---

Summary

* Problem: Gateway was pointing to non-existent services (prod456, carxyz) -> 503 for /products & /cart.
* Solution: Update gateway.py (or the deployment env) to use products and cart service names.
* Result: Product listing and cart pages load correctly again.

If you run into any other symptoms after the fix, let me know and we can dig deeper into traces or metrics.

