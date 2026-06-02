from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from state import GraphState

# ── System prompts ────────────────────────────────────────────────────────────

PROMPTS = {
    "kubernetes": """\
You are a Kubernetes specialist.
Use the available tools to investigate the cluster state: pods, deployments, services,
configmaps, events, and resources.
Focus strictly on the assigned task. Use only the minimum tools required to answer it —
do not call extra tools or fetch details that were not requested.
Be objective and include names, namespaces, and statuses in your responses.
When there are no more tool calls to make, provide a clear summary of your findings.""",

    "traces": """\
You are a distributed tracing specialist using Jaeger.
Use the available tools to investigate request traces: latencies, errors, and
service dependencies.
Focus strictly on the assigned task. Use only the minimum tools required to answer it —
do not explore beyond what was explicitly requested.
Be objective and include service names, operation names, durations, and relevant span
details in your responses.
When there are no more tool calls to make, provide a clear summary of your findings.""",

    "logs": """\
You are a Kubernetes log analysis specialist.
Use the available tools to search and analyze container logs: errors, warnings,
stack traces, and anomalous patterns.
Focus strictly on the assigned task. Use only the minimum tools required to answer it —
do not fetch logs from containers that were not requested.
Be objective and include timestamps, container names, and relevant log excerpts.
When there are no more tool calls to make, provide a clear summary of your findings.""",

    "metrics": """\
You are an infrastructure metrics specialist using Prometheus.
Use the available tools to investigate metrics: CPU, memory, latency, error rates,
and availability.
Focus strictly on the assigned task. Use only the minimum tools required to answer it —
do not query additional metrics that were not requested.
Be objective and include values, time ranges, and relevant trends.
When there are no more tool calls to make, provide a clear summary of your findings.""",
}


# ── Factory ───────────────────────────────────────────────────────────────────

def make_agent(name: str, llm, tools: list):
    """
    Returns a LangGraph node function for the given specialist agent.

    Each call to this node invokes the LLM exactly once.
    The tool-execution loop is handled externally by LangGraph's ToolNode:
      agent_node → ToolNode (executes tools) → agent_node → ... → orchestrator
    """
    bound_llm   = llm.bind_tools(tools)
    system_msg  = SystemMessage(PROMPTS[name])
    history_key = f"{name}_tool_history"
    answer_key  = f"{name}_answer"

    def agent_node(state: GraphState) -> dict:
        history = state.get(history_key) or []

        if not history:
            # First call: build messages from the orchestrator's task
            task = state.get("task_for_agent") or "Investigate the cluster and report your findings."
            messages = [system_msg, HumanMessage(task)]
        else:
            # Continuation after ToolNode returned results: reconstruct full context
            task = state.get("task_for_agent") or ""
            messages = [system_msg, HumanMessage(task)] + list(history)

        response = bound_llm.invoke(messages)

        update: dict = {history_key: [response]}

        if not getattr(response, "tool_calls", None):
            # No more tool calls — agent has finished its investigation
            update[answer_key]  = response.content
            update["last_agent"] = name

        return update

    agent_node.__name__ = f"{name}_agent"
    return agent_node
