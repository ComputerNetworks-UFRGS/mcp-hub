import os
import sys
from fastmcp import FastMCP
from pydantic import BaseModel
from typing import Optional
import subprocess
import logging
import json
import re
from datetime import datetime, timedelta
from kubernetes import client, config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

# Create MCP server for logs and list
mcp = FastMCP("k8s_logs_mcp")

# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────

def run_kubectl(command: list, timeout: int = 60) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return result.stderr
        return result.stdout
    except subprocess.TimeoutExpired:
        return "ERROR: kubectl command timed out after 60s"
    except Exception as e:
        return str(e)


def run_kubectl_json(command: list) -> dict:
    command += ["-o", "json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"error": result.stderr}
        return json.loads(result.stdout)
    except Exception as e:
        return {"error": str(e)}

def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@mcp.tool()
def root():
    """Health check — returns server status"""
    return {"status": "ok", "message": "Kubernetes Logs & Analysis MCP is running", "timestamp": now_iso()}


# ─────────────────────────────────────────────
# 1. BASIC
# ─────────────────────────────────────────────

@mcp.tool()
def list_pods(namespace: str = "default"):
    """List pods in a namespace with detailed status"""
    logger.info(f"Listing pods in namespace {namespace}")
    output = run_kubectl(["kubectl", "get", "pods", "-n", namespace, "-o", "wide"])
    return {"output": output, "namespace": namespace, "timestamp": now_iso()}


@mcp.tool()
def get_pod_logs(pod_name: str, namespace: str = "default", lines: int = 50, container: Optional[str] = None):
    """Returns logs from a pod (supports multiple containers)"""
    logger.info(f"Fetching logs for pod {pod_name}")
    cmd = ["kubectl", "logs", pod_name, "-n", namespace, f"--tail={lines}"]
    if container:
        cmd += ["-c", container]
    output = run_kubectl(cmd)
    return {"output": output, "pod": pod_name, "namespace": namespace, "lines": lines}


@mcp.tool()
def describe_pod(pod_name: str, namespace: str = "default"):
    """Describes a pod with all events and conditions"""
    logger.info(f"Describing pod {pod_name}")
    output = run_kubectl(["kubectl", "describe", "pod", pod_name, "-n", namespace])
    return {"output": output}


@mcp.tool()
def cluster_info():
    """General cluster information"""
    output = run_kubectl(["kubectl", "cluster-info"])
    version = run_kubectl(["kubectl", "version", "--short"])
    nodes = run_kubectl(["kubectl", "get", "nodes", "-o", "wide"])
    return {"cluster_info": output, "version": version, "nodes": nodes, "timestamp": now_iso()}

@mcp.tool()
def list_deployments(namespace: str = "default"):
    """List all deployments in a namespace"""
    logger.info(f"Listing deployments in namespace {namespace}")
    output = run_kubectl(["kubectl", "get", "deployments", "-n", namespace, "-o", "wide"])
    return {"output": output, "namespace": namespace, "timestamp": now_iso()}

@mcp.tool()
def list_namespaces():
    """List all namespaces in the cluster"""
    logger.info("Listing namespaces")
    output = run_kubectl(["kubectl", "get", "namespaces", "-o", "wide"])
    return {"output": output, "timestamp": now_iso()}

@mcp.tool()
def list_services(namespace: str = "default"):
    """List all services in a namespace"""
    logger.info(f"Listing services in namespace {namespace}")
    output = run_kubectl(["kubectl", "get", "services", "-n", namespace, "-o", "wide"])
    return {"output": output, "namespace": namespace, "timestamp": now_iso()}

@mcp.tool()
def list_nodes():
    """List all nodes in the cluster"""
    logger.info("Listing nodes")
    output = run_kubectl(["kubectl", "get", "nodes", "-o", "wide"])
    return {"output": output, "timestamp": now_iso()}

@mcp.tool()
def exec_pod_command(pod_name: str, command: str, namespace: str = "default", container: Optional[str] = None):
    """Execute a command inside a pod"""
    logger.info(f"Executing command in pod {pod_name}")
    cmd = ["kubectl", "exec", pod_name, "-n", namespace]
    if container:
        cmd += ["-c", container]
    cmd += ["--", "sh", "-c", command]
    output = run_kubectl(cmd)
    return {"output": output, "pod": pod_name, "command": command, "namespace": namespace}

# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    transport = os.getenv("MCP_TRANSPORT", "sse")
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8081"))

    logger.info(f"Starting k8s_logs_mcp.py MCP | transport={transport} host={host} port={port}")

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # FastMCP 2.x expõe o app Starlette assim:
        uvicorn.run(mcp.http_app(), host=host, port=port)
