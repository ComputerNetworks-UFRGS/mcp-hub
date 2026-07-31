import os
import sys
from contextvars import ContextVar
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional
import logging
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes config")
except config.ConfigException as e:
    logger.warning(f"Could not load in-cluster config: {e}")

mcp = FastMCP("k8s_mcp")

_impersonate_user: ContextVar[str] = ContextVar("impersonate_user", default="")


class _ImpersonateMiddleware(BaseHTTPMiddleware):
    """Read X-Remote-User header and store in ContextVar for impersonation."""
    async def dispatch(self, request, call_next):
        user = request.headers.get("x-remote-user", "")
        if user:
            logger.info(f"X-Remote-User header received: {user}")
        else:
            logger.warning(f"X-Remote-User header missing on {request.method} {request.url.path}")
        token = _impersonate_user.set(user)
        try:
            return await call_next(request)
        finally:
            _impersonate_user.reset(token)


class _HostNormalizer:
    """Rewrite Host header to localhost so FastMCP origin check passes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["headers"] = [
                (b"host", b"localhost") if k.lower() == b"host" else (k, v)
                for k, v in scope["headers"]
            ]
        await self.app(scope, receive, send)


# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────

def _k8s() -> client.ApiClient:
    """Return an ApiClient with impersonation headers if a user is set in the current context."""
    user = _impersonate_user.get()
    api = client.ApiClient()
    if not user:
        raise RuntimeError("X-Remote-User header ausente — chamada recusada sem impersonation")
    logger.info(f"Impersonating user: {user}")
    api.set_default_header("Impersonate-User", user)
    return api


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_resource_value(value: str) -> float:
    if not value or value in ("<unknown>", "N/A"):
        return 0.0
    if value.endswith("m"):
        return float(value[:-1]) / 1000
    if value.endswith("Ki"):
        return float(value[:-2]) / 1024
    if value.endswith("Mi"):
        return float(value[:-2])
    if value.endswith("Gi"):
        return float(value[:-2]) * 1024
    try:
        return float(value)
    except Exception:
        return 0.0


def _container_state(state) -> str:
    if state is None:
        return "unknown"
    if state.running:
        ts = state.running.started_at
        return f"running since {ts.isoformat() if ts else 'unknown'}"
    if state.waiting:
        reason = state.waiting.reason or "waiting"
        msg = f": {state.waiting.message}" if state.waiting.message else ""
        return f"{reason}{msg}"
    if state.terminated:
        t = state.terminated
        return f"terminated (exit {t.exit_code}, {t.reason or 'unknown'})"
    return "unknown"


def _cond_true(conds_map: dict, key: str) -> bool:
    c = conds_map.get(key)
    return c.status == "True" if c else False


def _pod_events(v1: client.CoreV1Api, pod_name: str, namespace: str) -> list:
    try:
        ev_list = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod"
        )
    except ApiException:
        return []

    def _ts(e):
        ts = e.last_timestamp
        if ts is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    return [
        {
            "type": e.type or "",
            "reason": e.reason or "",
            "message": e.message or "",
            "count": e.count or 1,
            "last_time": str(e.last_timestamp) if e.last_timestamp else "",
        }
        for e in sorted(ev_list.items, key=_ts)
    ]


def _get_pod_metrics(namespace: str) -> dict:
    """Returns {pod_name: {cpu_usage_raw, mem_usage_raw, cpu_usage_cores}} via metrics-server."""
    custom = client.CustomObjectsApi(_k8s())
    try:
        data = custom.list_namespaced_custom_object(
            group="metrics.k8s.io", version="v1beta1",
            namespace=namespace, plural="pods"
        )
    except ApiException:
        return {}
    result = {}
    for m in data.get("items", []):
        name = m["metadata"]["name"]
        containers = m.get("containers", [])
        cpu_raw = containers[0]["usage"]["cpu"] if containers else "0m"
        mem_raw = containers[0]["usage"]["memory"] if containers else "0Ki"
        result[name] = {
            "cpu_usage_raw": cpu_raw,
            "mem_usage_raw": mem_raw,
            "cpu_cores": parse_resource_value(cpu_raw),
        }
    return result


def _get_node_metrics() -> dict:
    """Returns {node_name: {cpu_usage, mem_usage}} via metrics-server."""
    custom = client.CustomObjectsApi(_k8s())
    try:
        data = custom.list_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="nodes"
        )
    except ApiException:
        return {}
    return {
        m["metadata"]["name"]: {
            "cpu_usage": m["usage"]["cpu"],
            "mem_usage": m["usage"]["memory"],
        }
        for m in data.get("items", [])
    }


# ─────────────────────────────────────────────
# 1. HEALTH CHECK
# ─────────────────────────────────────────────

@mcp.tool()
def root():
    """Health check — returns server status"""
    return {"status": "ok", "message": "Kubernetes Observability MCP v3 is running", "timestamp": now_iso()}


# ─────────────────────────────────────────────
# 2. BASIC POD / LOG / DESCRIBE
# ─────────────────────────────────────────────

@mcp.tool()
def list_pods(namespace: str = "default"):
    """List pods in a namespace with status, restarts, IP, and node"""
    logger.info(f"Listing pods in namespace {namespace}")
    v1 = client.CoreV1Api(_k8s())
    try:
        pod_list = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}", "namespace": namespace}

    pods = []
    for pod in pod_list.items:
        statuses = pod.status.container_statuses or []
        ready_count = sum(1 for cs in statuses if cs.ready)
        containers = len(pod.spec.containers)
        restarts = sum(cs.restart_count for cs in statuses)
        pods.append({
            "name": pod.metadata.name,
            "ready": f"{ready_count}/{containers}",
            "status": pod.status.phase or "Unknown",
            "restarts": restarts,
            "ip": pod.status.pod_ip or "",
            "node": pod.spec.node_name or "",
            "created": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else "",
        })
    return {"namespace": namespace, "pods": pods, "count": len(pods), "timestamp": now_iso()}


@mcp.tool()
def get_pod_logs(pod_name: str, namespace: str = "default", lines: int = 50, container: Optional[str] = None):
    """Returns logs from a pod (supports multiple containers via container= param)"""
    logger.info(f"Fetching logs for pod {pod_name}")
    v1 = client.CoreV1Api(_k8s())
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace,
            tail_lines=lines, container=container
        )
        return {"output": logs, "pod": pod_name, "namespace": namespace, "lines": lines}
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}", "pod": pod_name}


@mcp.tool()
def describe_pod(pod_name: str, namespace: str = "default"):
    """Full pod description: spec, container states, conditions, volumes, and events
    (equivalent to kubectl describe pod — uses Kubernetes API directly)"""
    logger.info(f"Describing pod {pod_name}")
    v1 = client.CoreV1Api(_k8s())
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    cs_map = {cs.name: cs for cs in (pod.status.container_statuses or [])}
    ics_map = {cs.name: cs for cs in (pod.status.init_container_statuses or [])}

    def _container_info(c, status_map):
        cs = status_map.get(c.name)
        res = c.resources or client.V1ResourceRequirements()
        last_state = _container_state(cs.last_state if cs else None) if cs and cs.last_state else None
        return {
            "name": c.name,
            "image": c.image,
            "ports": [f"{p.container_port}/{p.protocol or 'TCP'}" for p in (c.ports or [])],
            "state": _container_state(cs.state if cs else None),
            "last_state": last_state,
            "ready": cs.ready if cs else False,
            "restarts": cs.restart_count if cs else 0,
            "resources": {
                "requests": dict(res.requests) if res.requests else {},
                "limits": dict(res.limits) if res.limits else {},
            },
            "env": [
                {"name": e.name, "value": e.value or "(valueFrom)"}
                for e in (c.env or [])
            ],
        }

    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "node": pod.spec.node_name,
        "labels": dict(pod.metadata.labels or {}),
        "annotations": {
            k: v for k, v in (pod.metadata.annotations or {}).items()
            if not k.startswith("kubectl.kubernetes.io/last-applied")
        },
        "phase": pod.status.phase,
        "ip": pod.status.pod_ip,
        "service_account": pod.spec.service_account_name,
        "priority_class": pod.spec.priority_class_name,
        "init_containers": [_container_info(c, ics_map) for c in (pod.spec.init_containers or [])],
        "containers": [_container_info(c, cs_map) for c in pod.spec.containers],
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (pod.status.conditions or [])
        ],
        "volumes": [v.name for v in (pod.spec.volumes or [])],
        "events": _pod_events(v1, pod_name, namespace),
    }


# ─────────────────────────────────────────────
# 3. CLUSTER INFO / METRICS
# ─────────────────────────────────────────────

@mcp.tool()
def cluster_info():
    """General cluster information: nodes, versions, roles"""
    v1 = client.CoreV1Api(_k8s())
    try:
        node_list = v1.list_node()
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    nodes = []
    for n in node_list.items:
        conds = {c.type: c for c in (n.status.conditions or [])}
        roles = [
            k.replace("node-role.kubernetes.io/", "")
            for k in (n.metadata.labels or {})
            if k.startswith("node-role.kubernetes.io/")
        ]
        ni = n.status.node_info
        nodes.append({
            "name": n.metadata.name,
            "ready": conds["Ready"].status if "Ready" in conds else "Unknown",
            "roles": roles or ["<none>"],
            "kubelet_version": ni.kubelet_version if ni else "",
            "os": ni.os_image if ni else "",
            "architecture": ni.architecture if ni else "",
        })
    return {"nodes": nodes, "node_count": len(nodes), "timestamp": now_iso()}


@mcp.tool()
def metrics():
    """Current CPU and memory usage for nodes and pods (requires metrics-server)"""
    return {
        "nodes": _get_node_metrics(),
        "timestamp": now_iso(),
        "note": "Empty dict means metrics-server is unavailable",
    }


# ─────────────────────────────────────────────
# 4. POD RESOURCE SNAPSHOTS
# ─────────────────────────────────────────────

@mcp.tool()
def get_pod_resource_history(namespace: str = "default", hours: int = 1):
    """CPU/memory snapshot for all pods: requests, limits, current usage, throttle risk.
    Requires metrics-server for usage data; still returns pod specs without it."""
    logger.info(f"Resource history for namespace {namespace}")
    v1 = client.CoreV1Api(_k8s())
    try:
        pod_list = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    usage_map = _get_pod_metrics(namespace)
    result = []
    for pod in pod_list.items:
        pod_name = pod.metadata.name
        cpu_req = mem_req = cpu_lim = mem_lim = "N/A"
        for c in pod.spec.containers:
            if c.resources:
                reqs = c.resources.requests or {}
                lims = c.resources.limits or {}
                cpu_req = reqs.get("cpu", "N/A")
                mem_req = reqs.get("memory", "N/A")
                cpu_lim = lims.get("cpu", "N/A")
                mem_lim = lims.get("memory", "N/A")

        usage = usage_map.get(pod_name, {})
        cpu_use_cores = usage.get("cpu_cores", 0.0)
        cpu_lim_cores = parse_resource_value(cpu_lim)

        throttle_risk = "unknown"
        if cpu_lim_cores > 0 and usage:
            ratio = cpu_use_cores / cpu_lim_cores
            throttle_risk = "high" if ratio > 0.85 else "medium" if ratio > 0.60 else "low"

        result.append({
            "pod": pod_name,
            "status": pod.status.phase or "Unknown",
            "cpu_usage": usage.get("cpu_usage_raw", "N/A"),
            "mem_usage": usage.get("mem_usage_raw", "N/A"),
            "cpu_request": cpu_req,
            "cpu_limit": cpu_lim,
            "mem_request": mem_req,
            "mem_limit": mem_lim,
            "throttle_risk": throttle_risk,
            "snapshot_time": now_iso(),
        })

    return {
        "namespace": namespace,
        "pod_count": len(result),
        "metrics_available": bool(usage_map),
        "resources": result,
        "interpretation_hint": (
            "throttle_risk=high: usage above 85% of CPU limit — pod likely suffers throttling. "
            "OOM risk when mem_usage approaches mem_limit."
        )
    }


# ─────────────────────────────────────────────
# 5. RESTART HISTORY
# ─────────────────────────────────────────────

@mcp.tool()
def get_restart_timeline(namespace: str = "default"):
    """Restart history for all pods. Pods with many restarts indicate crashloops."""
    logger.info(f"Restart timeline for namespace {namespace}")
    v1 = client.CoreV1Api(_k8s())
    try:
        pod_list = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    restarts = []
    for pod in pod_list.items:
        statuses = pod.status.container_statuses or []
        total = sum(cs.restart_count for cs in statuses)
        last_state_info = []
        for cs in statuses:
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                last_state_info.append({
                    "container": cs.name,
                    "exit_code": t.exit_code,
                    "reason": t.reason,
                    "finished_at": str(t.finished_at) if t.finished_at else None,
                })

        severity = "critical" if total >= 10 else "warning" if total >= 3 else "ok"
        restarts.append({
            "pod": pod.metadata.name,
            "phase": pod.status.phase or "Unknown",
            "total_restarts": total,
            "severity": severity,
            "last_termination": last_state_info,
        })

    restarts.sort(key=lambda x: x["total_restarts"], reverse=True)
    return {
        "namespace": namespace,
        "timestamp": now_iso(),
        "pods": restarts,
        "summary": {
            "critical_pods": sum(1 for p in restarts if p["severity"] == "critical"),
            "warning_pods": sum(1 for p in restarts if p["severity"] == "warning"),
            "healthy_pods": sum(1 for p in restarts if p["severity"] == "ok"),
        },
        "interpretation_hint": (
            "exit_code=137 = OOMKill. exit_code=1 = application error. exit_code=143 = SIGTERM."
        )
    }


# ─────────────────────────────────────────────
# 6. CLUSTER EVENTS
# ─────────────────────────────────────────────

@mcp.tool()
def get_events_timeline(namespace: str = "default", event_type: Optional[str] = None):
    """Recent cluster events sorted by timestamp. event_type: 'Warning' or 'Normal'"""
    logger.info(f"Events timeline for namespace {namespace}")
    v1 = client.CoreV1Api(_k8s())
    try:
        ev_list = v1.list_namespaced_event(namespace=namespace)
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    def _ts(e):
        ts = e.last_timestamp
        if ts is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    events = []
    for ev in sorted(ev_list.items, key=_ts):
        if event_type and ev.type != event_type:
            continue
        events.append({
            "type": ev.type or "",
            "reason": ev.reason or "",
            "message": ev.message or "",
            "object": ev.involved_object.name if ev.involved_object else "",
            "kind": ev.involved_object.kind if ev.involved_object else "",
            "count": ev.count or 1,
            "first_time": str(ev.first_timestamp) if ev.first_timestamp else "",
            "last_time": str(ev.last_timestamp) if ev.last_timestamp else "",
        })

    warnings = [e for e in events if e["type"] == "Warning"]
    return {
        "namespace": namespace,
        "total_events": len(events),
        "warning_count": len(warnings),
        "events": events,
        "top_warnings": warnings[:10],
        "interpretation_hint": (
            "Correlate last_time of warnings with metric spikes. "
            "Critical reasons: OOMKilling, Evicted, BackOff, FailedScheduling."
        )
    }


# ─────────────────────────────────────────────
# 7. HPA STATUS
# ─────────────────────────────────────────────

@mcp.tool()
def get_hpa_status(namespace: str = "default"):
    """HPA status: current vs min/max replicas and scaling pressure"""
    logger.info(f"HPA status for namespace {namespace}")
    try:
        hpa_list = client.AutoscalingV2Api(_k8s()).list_namespaced_horizontal_pod_autoscaler(namespace=namespace)
    except ApiException:
        try:
            hpa_list = client.AutoscalingV1Api(_k8s()).list_namespaced_horizontal_pod_autoscaler(namespace=namespace)
        except ApiException as e:
            return {"error": f"{e.status}: {e.reason}"}

    hpas = []
    for hpa in hpa_list.items:
        current = hpa.status.current_replicas or 0
        desired = hpa.status.desired_replicas or 0
        min_r = hpa.spec.min_replicas or 1
        max_r = hpa.spec.max_replicas or 1
        at_max = current >= max_r

        if desired > current:
            pressure = "scaling_up"
        elif desired < current:
            pressure = "scaling_down"
        elif at_max:
            pressure = "saturated_at_max"
        else:
            pressure = "none"

        hpas.append({
            "name": hpa.metadata.name,
            "min_replicas": min_r,
            "max_replicas": max_r,
            "current_replicas": current,
            "desired_replicas": desired,
            "scaling_pressure": pressure,
            "at_maximum": at_max,
            "last_scale_time": str(hpa.status.last_scale_time) if hpa.status.last_scale_time else "N/A",
        })

    return {
        "namespace": namespace,
        "timestamp": now_iso(),
        "hpas": hpas,
        "saturated_hpas": [h for h in hpas if h["at_maximum"]],
        "interpretation_hint": (
            "saturated_at_max: HPA wants more replicas but hit its limit — capacity bottleneck."
        )
    }


# ─────────────────────────────────────────────
# 8. NODE PRESSURE
# ─────────────────────────────────────────────

@mcp.tool()
def get_node_pressure():
    """Node pressure conditions: MemoryPressure, DiskPressure, PIDPressure + current usage"""
    logger.info("Node pressure check")
    v1 = client.CoreV1Api(_k8s())
    try:
        node_list = v1.list_node()
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    top_map = _get_node_metrics()
    nodes = []
    for node in node_list.items:
        conds = {c.type: c for c in (node.status.conditions or [])}
        ready_cond = conds.get("Ready")
        usage = top_map.get(node.metadata.name, {})
        nodes.append({
            "node": node.metadata.name,
            "ready": ready_cond.status if ready_cond else "Unknown",
            "memory_pressure": _cond_true(conds, "MemoryPressure"),
            "disk_pressure": _cond_true(conds, "DiskPressure"),
            "pid_pressure": _cond_true(conds, "PIDPressure"),
            "has_any_pressure": any([
                _cond_true(conds, "MemoryPressure"),
                _cond_true(conds, "DiskPressure"),
                _cond_true(conds, "PIDPressure"),
            ]),
            "cpu_usage": usage.get("cpu_usage", "N/A"),
            "mem_usage": usage.get("mem_usage", "N/A"),
        })

    return {
        "timestamp": now_iso(),
        "nodes": nodes,
        "nodes_with_pressure": [n for n in nodes if n["has_any_pressure"]],
        "nodes_not_ready": [n for n in nodes if n["ready"] != "True"],
        "interpretation_hint": (
            "MemoryPressure=True causes pod eviction. DiskPressure can cause image pull failures."
        )
    }


# ─────────────────────────────────────────────
# 9. RESOURCE PATTERN ANALYSIS
# ─────────────────────────────────────────────

@mcp.tool()
def analyze_resource_patterns(namespace: str = "default"):
    """Detects idle, over-provisioned, OOM-risk, and CPU-throttling pods"""
    logger.info(f"Analyzing resource patterns for namespace {namespace}")
    data = get_pod_resource_history(namespace=namespace)
    if "error" in data:
        return data

    throttle_risk, oom_risk, idle, over_prov, healthy = [], [], [], [], []

    for pod in data.get("resources", []):
        cpu_use = parse_resource_value(pod.get("cpu_usage", "0"))
        mem_use = parse_resource_value(pod.get("mem_usage", "0"))
        cpu_lim = parse_resource_value(pod.get("cpu_limit", "0"))
        mem_lim = parse_resource_value(pod.get("mem_limit", "0"))
        cpu_req = parse_resource_value(pod.get("cpu_request", "0"))
        entry = {
            "pod": pod["pod"],
            "cpu_usage": pod.get("cpu_usage"),
            "mem_usage": pod.get("mem_usage"),
        }

        if cpu_lim > 0 and cpu_use / cpu_lim > 0.85:
            entry["issue"] = f"Throttling risk: {pod.get('cpu_usage')} of {pod.get('cpu_limit')} limit"
            throttle_risk.append(entry)
        elif mem_lim > 0 and mem_use / mem_lim > 0.85:
            entry["issue"] = f"OOM risk: {pod.get('mem_usage')} of {pod.get('mem_limit')} limit"
            oom_risk.append(entry)
        elif cpu_req > 0 and cpu_use < cpu_req * 0.10:
            entry["issue"] = f"Idle: using {pod.get('cpu_usage')} with request {pod.get('cpu_request')}"
            idle.append(entry)
        elif cpu_lim > 0 and cpu_req > 0 and (cpu_lim / max(cpu_req, 0.001)) > 10:
            entry["issue"] = f"Over-provisioned: request={pod.get('cpu_request')} limit={pod.get('cpu_limit')}"
            over_prov.append(entry)
        else:
            healthy.append(entry)

    recs = []
    if throttle_risk:
        recs.append(f"{len(throttle_risk)} pod(s) at CPU throttling risk — increase cpu.limit or optimize.")
    if oom_risk:
        recs.append(f"{len(oom_risk)} pod(s) at OOMKill risk — increase memory.limit.")
    if idle:
        recs.append(f"{len(idle)} pod(s) consuming <10% of cpu.request — reduce requests to free capacity.")
    if over_prov:
        recs.append(f"{len(over_prov)} over-provisioned pod(s) — request/limit ratio too high.")
    if not recs:
        recs.append("Resources appear well-sized.")

    return {
        "namespace": namespace,
        "timestamp": now_iso(),
        "summary": {
            "total": len(throttle_risk) + len(oom_risk) + len(idle) + len(over_prov) + len(healthy),
            "throttle_risk": len(throttle_risk),
            "oom_risk": len(oom_risk),
            "idle": len(idle),
            "over_provisioned": len(over_prov),
            "healthy": len(healthy),
        },
        "throttle_risk_pods": throttle_risk,
        "oom_risk_pods": oom_risk,
        "idle_pods": idle,
        "over_provisioned_pods": over_prov,
        "recommendations": recs,
    }


# ─────────────────────────────────────────────
# 10. EVENT + METRIC CORRELATION (Root Cause)
# ─────────────────────────────────────────────

@mcp.tool()
def correlate_events_and_resources(namespace: str = "default"):
    """Correlates Warning events with resource usage for root cause analysis"""
    logger.info(f"Correlating events and resources in namespace {namespace}")

    events_data = get_events_timeline(namespace=namespace, event_type="Warning")
    resources_data = get_pod_resource_history(namespace=namespace)
    restarts_data = get_restart_timeline(namespace=namespace)

    problem_pods = {ev["object"] for ev in events_data.get("events", [])}
    problem_pods |= {
        p["pod"] for p in restarts_data.get("pods", [])
        if p["severity"] in ("warning", "critical")
    }

    correlated = []
    for pod_res in resources_data.get("resources", []):
        pod_name = pod_res["pod"]
        if pod_name not in problem_pods and pod_res.get("throttle_risk") not in ("high", "medium"):
            continue
        pod_events = [e for e in events_data.get("events", []) if e["object"] == pod_name]
        pod_restarts = next((p for p in restarts_data.get("pods", []) if p["pod"] == pod_name), {})
        last_term = (pod_restarts.get("last_termination") or [{}])[0]
        correlated.append({
            "pod": pod_name,
            "cpu_usage": pod_res.get("cpu_usage"),
            "mem_usage": pod_res.get("mem_usage"),
            "throttle_risk": pod_res.get("throttle_risk"),
            "restart_count": pod_restarts.get("total_restarts", 0),
            "restart_severity": pod_restarts.get("severity", "ok"),
            "last_termination_reason": last_term.get("reason"),
            "recent_warnings": [
                {"reason": e["reason"], "message": e["message"][:100], "time": e["last_time"]}
                for e in pod_events[:5]
            ],
        })

    correlated.sort(key=lambda x: (-x["restart_count"], 0 if x["throttle_risk"] == "high" else 1))

    return {
        "namespace": namespace,
        "timestamp": now_iso(),
        "investigation_summary": {
            "pods_with_issues": len(correlated),
            "total_warnings": events_data.get("warning_count", 0),
            "critical_restart_pods": restarts_data.get("summary", {}).get("critical_pods", 0),
        },
        "correlated_issues": correlated,
        "interpretation_hint": (
            "Pods with high restart_count + OOMKill = memory leak or limit too low. "
            "throttle_risk=high + BackOff events = CPU overload."
        )
    }


# ─────────────────────────────────────────────
# 11. DEPLOYMENTS & ROLLOUTS
# ─────────────────────────────────────────────

@mcp.tool()
def get_deployment_status(namespace: str = "default"):
    """Status of all deployments: replicas, rollout progress, images"""
    logger.info(f"Deployment status for namespace {namespace}")
    apps = client.AppsV1Api(_k8s())
    try:
        dep_list = apps.list_namespaced_deployment(namespace=namespace)
    except ApiException as e:
        return {"error": f"{e.status}: {e.reason}"}

    deployments = []
    for dep in dep_list.items:
        desired = dep.spec.replicas or 0
        ready = dep.status.ready_replicas or 0
        available = dep.status.available_replicas or 0
        updated = dep.status.updated_replicas or 0
        images = [c.image for c in dep.spec.template.spec.containers]
        revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "N/A")
        deployments.append({
            "name": dep.metadata.name,
            "desired_replicas": desired,
            "ready_replicas": ready,
            "available_replicas": available,
            "updated_replicas": updated,
            "rollout_in_progress": updated != desired or ready != desired,
            "health": "degraded" if ready < desired else "healthy",
            "images": images,
            "revision": revision,
        })

    return {
        "namespace": namespace,
        "timestamp": now_iso(),
        "deployments": deployments,
        "degraded": [d for d in deployments if d["health"] == "degraded"],
        "rolling_out": [d for d in deployments if d["rollout_in_progress"]],
        "interpretation_hint": (
            "rollout_in_progress=True during an error spike = deploy may be the root cause."
        )
    }


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    transport = os.getenv("MCP_TRANSPORT", "streamable_http")
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8080"))
    logger.info(f"Starting k8s_mcp | transport={transport} host={host} port={port}")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        starlette_app = mcp.http_app()
        starlette_app.add_middleware(_ImpersonateMiddleware)
        uvicorn.run(_HostNormalizer(starlette_app), host=host, port=port)
