# Magentic
This project uses a Magentic Architecture with Task, Progress and Knowledge Ledgers:

Orchestrator Agent: Creates a plan in the Task Ledger, decides which agent is best suited.

K8s, (K8s) Logs, Jaeger and Prometheus Agents: Use MCP tools to answer the prompt.

Orchestrator Agent: Updates the Progress Ledger. If task is complete, gives an final answer.

Reflect Agent: Writes relevant information on a Session Ledger, used during a multi-turn conversation, and on a Persistent Ledger that is always used.
These Ledgers give additional context for the Orchestrator Agent.

# How to run

In the backend folder: 

```Create your .env based on .env_example```

```pip install -r requirements.txt```

```python main.py```

# User Interface

This directory also features an User Interface.

To run:


```bash
cd langgraph_agents/magentic/magentic_ui
pip install fastapi "uvicorn[standard]"
# All other dependencies come from magentic/requirements.txt
uvicorn app:app --reload
```

To build as a docker image, see deploy/README.md

