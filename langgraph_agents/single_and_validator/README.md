# Single and Validator Agent

This system uses a single agent with access to the K8s and Jaeger MCP servers and a validator agent, that checks if the answers are coherent.

# How to run

`Create your .env based on .env_example`
`pip install -r requirements.txt`
`python main.py`

(If you don't have a Langfuse endpoint, the system will show error messages, but will stil work.)