# Orquestrador
This project uses a simple Router Architecture:

Orchestrator Agent: Decides which agent is best suited for the prompt.

K8s and Jaeger Agents: Use MCP tools to answer the prompt.

Orchestrator Agent: Summarizes the result.

This version uses prompts from Igor's n8n workflow.

# How to run

`Create your .env based on .env_example`
`pip install -r requirements.txt`
`python main.py`

(If you don't have a Langfuse endpoint, the system will show error messages, but will stil work.)