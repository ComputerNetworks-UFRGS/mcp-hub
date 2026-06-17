import asyncio
import json
import logging
import os
import re
import shlex
import httpx
import subprocess
from datetime import datetime, timedelta
from typing import Any, Optional
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("prometheus-promql-mcp")

# ---------------------------------------------------------------------------
# Config — override via K8s ConfigMap / env vars
# ---------------------------------------------------------------------------
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-operated.monitoring.svc.cluster.local:9090")
KUBECTL_PATH   = os.getenv("KUBECTL_PATH", "/usr/local/bin/kubectl")
PROMTOOL_PATH  = os.getenv("PROMTOOL_PATH", "/usr/local/bin/promtool")
KUBECONFIG     = os.getenv("KUBECONFIG", "")
DEFAULT_NS     = os.getenv("DEFAULT_NAMESPACE", "default")
MCP_HOST       = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT       = int(os.getenv("MCP_PORT", "8002"))
MCP_TRANSPORT  = os.getenv("MCP_TRANSPORT", "sse")   # 'sse' or 'stdio'

mcp = FastMCP("prometheus-promql-mcp")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(cmd: str, timeout: int = 30) -> dict:
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **({"KUBECONFIG": KUBECONFIG} if KUBECONFIG else {})},
        )
        return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip(), "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s", "returncode": -1}
    except FileNotFoundError as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": -1}


async def _prom_get(path: str,  params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(f"{PROMETHEUS_URL}{path}", params=params or {})
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

async def _instant(expr: str, ) -> dict:
    return await _prom_get("/api/v1/query", {"query": expr})


async def _range(  expr: str, minutes: int = 30, step: str = "60s") -> dict:
    now   = datetime.utcnow()
    start = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return await _prom_get("/api/v1/query_range", {"query": expr, "start": start, "end": end, "step": step})

# ===========================================================================
# 5) GENERIC PROMETHEUS
# ===========================================================================

@mcp.tool()
async def prometheus_get_labels(metric: str) -> str:
    """Return all label names for a metric."""
    return json.dumps(await _prom_get("/api/v1/labels", {"match[]": metric}), indent=2)


@mcp.tool()
async def prometheus_get_label_values(label: str, metric: Optional[str] = None) -> str:
    """
    Return all values for a label, optionally scoped to a metric.

    Args:
        label:  Label name, e.g. 'namespace', 'pod', 'node'
        metric: Optional metric to scope results
    """
    params = {"match[]": metric} if metric else {}
    return json.dumps(await _prom_get(f"/api/v1/label/{label}/values", params), indent=2)


@mcp.tool()
async def prometheus_get_alerts() -> str:
    """Return all currently firing alerts from Prometheus."""
    return json.dumps(await _prom_get("/api/v1/alerts"), indent=2)


@mcp.tool()
async def prometheus_get_rules() -> str:
    """Return all loaded alerting and recording rules."""
    return json.dumps(await _prom_get("/api/v1/rules"), indent=2)


@mcp.tool()
async def prometheus_get_targets() -> str:
    """Return all scrape targets and their UP/DOWN health."""
    return json.dumps(await _prom_get("/api/v1/targets"), indent=2)


@mcp.tool()
async def prometheus_health_check() -> str:
    """Check Prometheus /-/healthy and /-/ready endpoints."""
    results = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for path in ("/-/healthy", "/-/ready"):
            try:
                r = await client.get(f"{PROMETHEUS_URL}{path}")
                results[path] = {"status_code": r.status_code, "body": r.text.strip()}
            except Exception as exc:
                results[path] = {"error": str(exc)}
    return json.dumps(results, indent=2)

# ===========================================================================
# 6) PROMTOOL + PROMQL HELPERS
# ===========================================================================

@mcp.tool()
def promtool_check_rules(rules_file_path: str) -> str:
    """Validate a Prometheus rules YAML file using promtool."""
    return json.dumps(_run(f"{PROMTOOL_PATH} check rules {rules_file_path}"), indent=2)


@mcp.tool()
def promtool_lint_rules(rules_file_path: str) -> str:
    """Lint a Prometheus rules YAML file for style issues."""
    return json.dumps(_run(f"{PROMTOOL_PATH} check rules --lint {rules_file_path}"), indent=2)


@mcp.tool()
def promtool_check_config(config_file_path: str) -> str:
    """Validate a prometheus.yml configuration file."""
    return json.dumps(_run(f"{PROMTOOL_PATH} check config {config_file_path}"), indent=2)


@mcp.tool()
def promtool_query_instant(expr: str) -> str:
    """Run an instant PromQL query via promtool CLI."""
    clean_expr = expr.replace("\\", "") #The agent sometimes adds extra backslashes when passing the query through the CLI, so we remove them here for promtool to work correctly.
    return json.dumps(_run(f"promtool query instant {PROMETHEUS_URL} {shlex.quote(clean_expr)}"), indent=2)


@mcp.tool()
def promtool_query_range(expr: str, start: str, end: str, step: str = "1m") -> str:
    """
    Run a range PromQL query via promtool CLI.

    Args:
        expr:  PromQL expression
        start: Start RFC3339, e.g. '2024-01-01T00:00:00Z'
        end:   End RFC3339
        step:  Resolution e.g. '1m', '5m'
    """
    clean_expr = expr.replace("\\", "") #The agent sometimes adds extra backslashes when passing the query through the CLI, so we remove them here for promtool to work correctly.
    cmd = f"{PROMTOOL_PATH} query range --start={start} --end={end} --step={step} {PROMETHEUS_URL}  {shlex.quote(clean_expr)}"
    return json.dumps(_run(cmd), indent=2)

# ===========================================================================
# Entrypoint
# ===========================================================================

if __name__ == "__main__":
    logger.info(f"Starting Simple Prometheus PromQL MCP | transport={MCP_TRANSPORT} host={MCP_HOST} port={MCP_PORT}")
    logger.info(f"Prometheus URL: {PROMETHEUS_URL} | Default namespace: {DEFAULT_NS}")

    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(mcp.http_app(), host=MCP_HOST, port=MCP_PORT)