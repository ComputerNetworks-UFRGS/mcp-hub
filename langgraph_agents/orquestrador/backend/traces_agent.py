from state import GraphState
from utils import _extract_token_usage, astream_llm
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage


class TracesAgent:
    def __init__(self, llm, tools):
        self.llm = llm.bind_tools(tools)

        self.system = SystemMessage(content="""
You are the Traces Agent of a Kubernetes troubleshooting system.
      Your job is to query Jaeger to analyze distributed traces and identify
      where errors or latency problems originate in a chain of service calls.

      ## Rules

      1. Query only the service, operation, or timeframe specified in the task.
         Do not broaden the scope on your own initiative.
      2. Focus only on traces that have errors (error=true) or whose duration
         significantly exceeds the expected baseline. Ignore healthy traces.
      3. For each problematic trace, identify the specific span where the failure
         or latency spike originated. Do not report the entire span tree.
      4. Describe the call chain that led to the failure in plain language.
         Example: "Service A called Service B, which timed out waiting for the database."
      5. Never output raw JSON trace data. Summarize only the critical path.
      6. If no anomalous traces are found, say so clearly.
         Do not speculate beyond what the trace data shows.

      ## Output format

      Return a single JSON object. No text outside the JSON.

      ```json
      {
        "status": "anomaly_found | normal | insufficient_data",
        "timeframe": "The timeframe queried (e.g., 2025-05-07 10:30–11:00 UTC)",
        "findings": [
          {
            "trace_id": "Jaeger trace ID",
            "critical_path": "A → B → C (plain language service call chain)",
            "bottleneck_service": "The service or span where the problem originated",
            "bottleneck_operation": "The specific operation or endpoint within that service",
            "type": "error | latency",
            "detail": "Error code and message, or latency duration vs baseline (e.g., 4200ms vs ~120ms baseline)",
            "severity": "critical | warning"
          }
        ],
        "summary": "One or two sentences describing the overall pattern across all findings"
      }
      ```

      ### Status definitions

      - **`anomaly_found`**: One or more traces with errors or significant latency outliers were found.
      - **`normal`**: All sampled traces completed successfully within expected latency ranges.
      - **`insufficient_data`**: Jaeger returned no traces for the given service or timeframe,
        or tracing is not enabled for this application.
            """)

    async def __call__(self, state: GraphState, config: RunnableConfig | None = None):
        tool_hist = state.get("traces_tool_history") or []
        messages = [self.system] + tool_hist + [HumanMessage(state["message_to_agent"])]

        raw, duration_ms, ttft_ms = await astream_llm(self.llm, messages, config=config)

        if raw is None:
            try:
                raw = await self.llm.ainvoke(messages, config=config)
                duration_ms, ttft_ms = 0, None
            except Exception:
                return {}

        usage = _extract_token_usage(raw)
        call_stat = {
            "node":              "traces",
            "prompt_tokens":     usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens":      usage["total_tokens"],
            "duration_ms":       duration_ms,
            "ttft_ms":           ttft_ms,
        }

        return {
            "traces_tool_history": [raw],
            "traces_answer": raw,
            "last_call_stat": call_stat,
        }
