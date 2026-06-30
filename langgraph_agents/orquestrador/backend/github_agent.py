from state import GraphState
from utils import _extract_token_usage, astream_llm
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage


class GithubAgent:
    def __init__(self, llm, tools):
        self.llm = llm.bind_tools(tools)

        self.system = SystemMessage(content="""
You are an application code specialist for minimal-boutique, a microservices e-commerce
platform developed by ComputerNetworks-UFRGS (github.com/ComputerNetworks-UFRGS/minimal-boutique).
Use the github tools to investigate the application code.
Focus strictly on the assigned task. Use only the minimum tools required to answer it.
Be objective.
When there are no more tool calls to make, provide a clear summary of your findings.

IMPORTANT — search scope:
All search tools (search_code, search_commits, search_issues, search_pull_requests)
search ALL of GitHub by default. You MUST always add the qualifier
  repo:ComputerNetworks-UFRGS/minimal-boutique
to every search query, otherwise results from unrelated repositories will be returned.
Example: instead of "payment service", use "payment service repo:ComputerNetworks-UFRGS/minimal-boutique".
""")

    async def __call__(self, state: GraphState, config: RunnableConfig | None = None):
        tool_hist = state.get("github_tool_history") or []
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
            "node":              "github",
            "prompt_tokens":     usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens":      usage["total_tokens"],
            "duration_ms":       duration_ms,
            "ttft_ms":           ttft_ms,
        }

        return {
            "github_tool_history": [raw],
            "github_answer": raw,
            "last_call_stat": call_stat,
        }
