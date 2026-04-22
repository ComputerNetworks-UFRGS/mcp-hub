# LangGraph Agents

A multi-agent LLM system built with LangGraph.

## Overview

This project uses a simple Router Architecture:

Router Agent: Decides which agent is best suited for the prompt.

K8s and Jaeger Agents: Use MCP tools to answer the prompt.

Answer Agent: Summarizes the result.

(Note that only one of the MCP agents is called at once. In future versions the Router Agent may call more than one agent at once.)

# How to run

`Create your .env based on .env_example`
`pip install -r requirements.txt`
`python main.py`

(If you don't have a Langfuse endpoint, the system will show error messages, but will stil work.)