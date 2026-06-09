import time
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, AIMessageChunk
from state import GraphState


def _extract_token_usage(response) -> dict:
    """
    Extract prompt/completion/total token counts from an LLM response.
    Handles both the standardized usage_metadata (langchain >= 0.2) and
    the OpenAI-style response_metadata fallback used by some providers.
    Returns zeros when the model does not report usage.
    """
    if response is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Standardized: AIMessage.usage_metadata (langchain-core >= 0.2)
    meta = getattr(response, "usage_metadata", None)
    if meta:
        inp  = meta.get("input_tokens",  0) or 0
        out  = meta.get("output_tokens", 0) or 0
        tot  = meta.get("total_tokens",  0) or (inp + out)
        return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": tot}

    # Fallback: OpenAI response_metadata (e.g. Ollama via ChatOpenAI)
    rm = getattr(response, "response_metadata", {}) or {}
    tu = rm.get("token_usage") or rm.get("usage") or {}
    return {
        "prompt_tokens":     tu.get("prompt_tokens",     0) or 0,
        "completion_tokens": tu.get("completion_tokens", 0) or 0,
        "total_tokens":      tu.get("total_tokens",      0) or 0,
    }

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

        t0 = time.monotonic()
        t_first: float | None = None
        error_msg = None
        response = None

        try:
            chunks = []
            for chunk in bound_llm.stream(messages):
                if t_first is None:
                    t_first = time.monotonic()
                chunks.append(chunk)
            if chunks:
                merged: AIMessageChunk = chunks[0]
                for c in chunks[1:]:
                    merged = merged + c
                # Convert to AIMessage for full LangGraph/ToolNode compatibility
                response = AIMessage(
                    content=merged.content,
                    tool_calls=list(merged.tool_calls),
                    usage_metadata=getattr(merged, "usage_metadata", None),
                    response_metadata=getattr(merged, "response_metadata", {}),
                    id=getattr(merged, "id", None),
                )
            else:
                error_msg = "Model returned empty stream"
        except Exception as e:
            error_msg = str(e)

        duration_ms = int((time.monotonic() - t0) * 1000)
        ttft_ms = int((t_first - t0) * 1000) if t_first is not None else None

        # Guard: local LLMs (Ollama/vLLM) can return None when the context is too
        # long or the server hits an error. Adding None to add_messages state causes
        # 'NoneType has no attribute model_dump'. Return a synthetic AIMessage instead.
        if response is None:
            msg = error_msg or "Model returned no response — context may be too long."
            response = AIMessage(content=f"[Agent error: {msg}]")

        usage  = _extract_token_usage(response)
        update: dict = {
            history_key: [response],
            "last_call_stat": {
                "node":               f"{name}_agent",
                "prompt_tokens":      usage["prompt_tokens"],
                "completion_tokens":  usage["completion_tokens"],
                "total_tokens":       usage["total_tokens"],
                "duration_ms":        duration_ms,
                "ttft_ms":            ttft_ms,   # time to first token (provider latency)
            },
        }

        if not getattr(response, "tool_calls", None):
            # No more tool calls — agent has finished its investigation
            update[answer_key]   = response.content
            update["last_agent"] = name

        return update

    agent_node.__name__ = f"{name}_agent"
    return agent_node
