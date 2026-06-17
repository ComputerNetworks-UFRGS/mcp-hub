Root cause

Pod template mis‑configuration – The products Deployment’s pod template contains two containers (products and postgres).

The PostgreSQL container is meant to run in its own (stateful) pod/service.
Running the database inside the same pod as the application creates a “sidecar” that consumes memory and is not replicated correctly.
Insufficient memory limits – The products container has:

limits: memory: 64Mi
requests: memory: 32Mi
The application (Flask + SQLAlchemy + OpenTelemetry + in‑memory DB sync) quickly exceeds 64 Mi, causing the container to be OOMKilled. The kubelet restarts it, leading to the observed CrashLoopBackOff status. Each restart incurs a pause, and the service is very slow when it is up.

Resulting latency & outages –

Crash‑looped pods cause intermittent timeouts for clients.
Even when a pod is running, the high memory churn (OOM→restart→restart…) keeps the container in a high‑resource state, so request handling is sluggish.
What you can do right now

Step        Why        Tooling / Commands
1. Separate PostgreSQL        PostgreSQL must run in its own Deployment/StatefulSet and be accessible via a Service.        kubectl delete pod products‑c856fc684‑q9wmt (to stop the faulty pod) <br> kubectl apply -f <postgres‑deployment.yaml>
2. Increase product memory limits        Give the app enough headroom while still being reasonable for the cluster.        kubectl patch deployment products -n minimal-boutique --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources","value":{"limits":{"memory":"256Mi","cpu":"50m"},"requests":{"memory":"128Mi","cpu":"10m"}}]'
3. Add proper liveness/readiness probes        Prevent traffic from going to a pod that is about to OOM‑kill.        Add probes in the Deployment spec: <br>yaml\nreadinessProbe:\n httpGet:\n path: /health\n port: 5001\n initialDelaySeconds: 10\n
4. Re‑apply Deployment        Force a new rollout with the correct pod spec.        kubectl rollout restart deployment products -n minimal-boutique
5. Verify        Ensure pods reach Running and stay stable.        kubectl get pods -n minimal-boutique -w
6. Monitor        Confirm that the OOMKilled events disappear.        kubectl describe pod <pod> -n minimal-boutique
7. Optionally enable horizontal scaling        If traffic increases, add an HPA based on CPU/memory.        kubectl autoscale deployment products -n minimal-boutique --cpu-percent=50 --min=1 --max=5

Quick sanity check
kubectl get pods -n minimal-boutique
kubectl describe pod products-c856fc684-44nwh -n minimal-boutique
kubectl logs products-c856fc684-44nwh -n minimal-boutique
You should see:

No more OOMKilled events.
Only one container (products) in the pod.
The pod’s status is Running and ready.

TL;DR
The products service was crashing because its pod was incorrectly configured to run both the app and a PostgreSQL container, and the app’s memory limit (64 Mi) was far too low for its workload. Fix the pod template, give the app more memory, add probes, and restart the deployment. Once those changes are in place, the service will become stable and request latency will drop to normal levels.