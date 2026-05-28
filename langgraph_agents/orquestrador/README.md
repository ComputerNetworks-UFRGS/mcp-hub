# Orquestrador
This project uses a simple Router Architecture:

Orchestrator Agent: Decides which agent is best suited for the prompt.

K8s, (K8s) Logs, Jaeger and Prometheus Agents: Use MCP tools to answer the prompt.

Orchestrator Agent: Summarizes the result.

# How to run

In the backend folder: 

```Create your .env based on .env_example```

```pip install -r requirements.txt```

```python main.py```

# User Interface

This directory also features an User Interface.

To run:


```bash
cd langgraph_agents/orquestrador/router_ui
pip install fastapi "uvicorn[standard]"
# All other dependencies come from orquestrador/requirements.txt
uvicorn app:app --reload
```

To build as a docker image, see deploy/README.md

