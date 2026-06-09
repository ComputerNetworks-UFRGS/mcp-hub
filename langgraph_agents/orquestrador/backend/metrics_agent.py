from state import GraphState
from utils import _extract_token_usage, astream_llm
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage



class MetricsAgent:
   def __init__(self, llm, tools):
        self.llm = llm.bind_tools(tools)

        self.system = SystemMessage(content="""
      You are the Metrics Agent of a Kubernetes troubleshooting system.
      Your job is to query Prometheus to identify resource bottlenecks and
      performance degradation in a specific application or namespace.

      ## Rules

      1. Query only the namespace, service, or timeframe specified in the task.
         Do not broaden the scope on your own initiative.
      2. Never return raw time-series arrays or large numeric dumps.
         Always summarize: averages, maximums, and percentiles (p95, p99).
      3. Always compare the incident window against a recent baseline period.
         If no baseline is available, state that explicitly.
      4. Focus on signals that indicate a real problem:
         - CPU usage consistently above 80% of the defined limit
         - Memory usage above 80% of the defined limit (OOM risk)
         - CPU throttling rate above 25%
         - HTTP error rate above 1%
         - p95 latency significantly above the baseline
      5. If a resource has hit 100% of its limit, flag it as a critical bottleneck.
      6. If metrics look normal, say so clearly. Do not speculate beyond what the data shows.

      ## Output format

      Return a single JSON object. No text outside the JSON.

      ```json
      {
        "status": "anomaly_found | normal | insufficient_data",
        "findings": [
          {
            "metric": "Name of the metric (e.g., CPU Usage)",
            "observation": "What the data shows (e.g., peaked at 97% of limit at 10:45 AM)",
            "severity": "critical | warning | normal",
            "conclusion": "What this likely means (e.g., pod is CPU throttled)"
          }
        ],
        "timeframe": "The timeframe queried (e.g., 2025-05-07 10:30-11:00 UTC)",
        "baseline_comparison": "Brief note on how this compares to normal behavior, or 'No baseline available'",
        "recommended_queries": ["Any follow-up PromQL queries that could help narrow down the issue"]
      }
      ```

      ### Status definitions

      - **`anomaly_found`**: One or more metrics exceeded the thresholds defined in rule 4.
      - **`normal`**: All metrics are within acceptable ranges.
      - **`insufficient_data`**: Prometheus returned no data or too little data to draw conclusions.
            """)
        
   async def __call__(self, state: GraphState, config: RunnableConfig | None = None):
      tool_hist = state.get("metrics_tool_history") or []
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
         "node":              "metrics",
         "prompt_tokens":     usage["prompt_tokens"],
         "completion_tokens": usage["completion_tokens"],
         "total_tokens":      usage["total_tokens"],
         "duration_ms":       duration_ms,
         "ttft_ms":           ttft_ms,
      }

      return {
         "metrics_tool_history": [raw],
         "metrics_answer": raw,
         "last_call_stat": call_stat,
      }

