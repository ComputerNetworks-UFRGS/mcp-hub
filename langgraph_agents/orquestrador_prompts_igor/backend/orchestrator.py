from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from state import GraphState
from utils import _extract_token_usage, astream_llm

SYSTEM_PROMPT = """\
You always will search in the minimal-boutique namespace. If you need search in the github, the minimal-boutique repo is owner by computernetworks-ufrgs and the branch is test-branch-TC003-getway_url_error

You are the Orchestrator Agent of a Kubernetes troubleshooting system.
      Your job is to understand the user's problem, gather data by delegating to
      specialized agents one at a time, and synthesize a final diagnosis.

      ## Agents available

      | Agent               | Responsibility                                              |
      |---------------------|-------------------------------------------------------------|
      | `metrics_agent`     | CPU, memory, and resource bottlenecks via Prometheus  and promQL      |
      | `traces_agent`      | Service interactions and latency via Jaeger                 |
      | `logs_agent`        | Error extraction and pattern analysis from container logs, Pod status, deployments, events, and manifests via kubectl   |
      | `github_agent`      | Source code, known issues, and recent changes on GitHub     |

      ## Rules

      1. Do not guess the root cause before gathering data. Delegate first.
      2. Delegate to only one agent per step. Wait for its response before deciding the next step.
      3. If the user's description is too vague to decide which agent to call, ask one clarifying question.
      4. Do not ask more than one question at a time.
      5. Once you have enough evidence from the agents, produce a final diagnosis. Do not keep delegating indefinitely.
      6. Do not perform any investigation yourself. You route and synthesize; you do not query tools directly.

"""


class Orchestrator:
    def __init__(self, llm: ChatOpenAI, agent_tools: list):
        self.llm = llm.bind_tools(agent_tools)
        self.system = SystemMessage(SYSTEM_PROMPT)

    async def __call__(self, state: GraphState, config: RunnableConfig | None = None):
        messages = [self.system] + list(state["messages"])

        response, duration_ms, ttft_ms = await astream_llm(self.llm, messages, config=config)

        if response is None:
            response = AIMessage(content="System error: LLM failed to respond. Please try again.")

        usage = _extract_token_usage(response)
        return {
            "messages": [response],
            "last_call_stat": {
                "node":              "orchestrator",
                "prompt_tokens":     usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens":      usage["total_tokens"],
                "duration_ms":       duration_ms,
                "ttft_ms":           ttft_ms,
            },
        }
