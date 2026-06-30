import random
import json

from state import GraphState
from utils import _extract_token_usage, _parse_json, astream_llm
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END


class Orchestrator:

    @staticmethod
    def choose_action(state: GraphState) -> str:
        if state["action"] == "delegate_to_agent":
            if state["target_agent"] in ["metrics_agent", "traces_agent", "logs_agent", "kubernetes_agent", "github_agent"]:
                return state["target_agent"]
        return "END"

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

        self.system = SystemMessage(content=
            """
You are the Orchestrator Agent of a Kubernetes troubleshooting system.
      Your job is to understand the user's problem, gather data by delegating to
      specialized agents one at a time, and synthesize a final diagnosis.

      ## Agents available

      | Agent               | Responsibility                                              |
      |---------------------|-------------------------------------------------------------|
      | `metrics_agent`     | CPU, memory, and resource bottlenecks via Prometheus        |
      | `traces_agent`      | Service interactions and latency via Jaeger                 |
      | `logs_agent`        | Error extraction and pattern analysis from container logs   |
      | `kubernetes_agent`  | Pod status, deployments, events, and manifests via kubectl  |
      | `github_agent`      | Application source code, commits, issues and PRs via GitHub |

      ## Rules

      1. Do not guess the root cause before gathering data. Delegate first.
      2. Delegate to only one agent per step. Wait for its response before deciding the next step.
      3. If the user's description is too vague to decide which agent to call, ask one clarifying question.
      4. Do not ask more than one question at a time.
      5. Once you have enough evidence from the agents, produce a final diagnosis. Do not keep delegating indefinitely.
      6. Do not perform any investigation yourself. You route and synthesize; you do not query tools directly.
      7. Do not try to call tools. Your entire response must be a raw JSON object written as plain text in your message content.

      ## Output format

      Every response must be a single JSON object. No text outside the JSON.

      ```json
      {
        "thought": "Brief reasoning about what to do next and why.",
        "action": "delegate_to_agent | ask_user | final_resolution",
        "target_agent": "agent_name or null",
        "message": "The prompt to send to the agent, or the message/question to the user."
      }
      ```

      ### Action definitions

      - **`delegate_to_agent`**: Send a focused investigation task to one agent.
        Set `target_agent` to the agent's name. Write `message` as a clear, specific
        task — include any relevant context the agent will need (namespace, timeframe,
        error message observed, etc.).
      - **`ask_user`**: Ask the user one clarifying question before proceeding.
        Set `target_agent` to null.
      - **`final_resolution`**: You have enough data to conclude. Set `target_agent`
        to null. Write `message` as a structured summary written in Markdown containing:
        - **Root cause**: What is failing and why.
        - **Supporting evidence**: Which agents reported what.
        - **Recommended actions**: Concrete steps to resolve the issue.


        # YOU MUST ALWAYS GENERATE AN OUTPUT OTHER THAN INTERNAL CHAIN OF THOUGHT REASONING. THE OUTPUT MUST NOT BE EMPTY.
        ## IMPORTANT
        You do NOT have any tools or functions available.
        Do not use function calling or tool_calls under any circumstances.
        Your entire response must be a raw JSON object written as plain text in your message content.
"""
        )

    async def __call__(self, state: GraphState, config: RunnableConfig | None = None):
        messages = [self.system] + state["messages"]

        raw_response, duration_ms, ttft_ms = await astream_llm(
            self.llm, messages, config=config, seed=random.randint(0, 99999)
        )

        if raw_response is None:
            return {
                "thought":          "System error: LLM failed to respond.",
                "action":           "final_resolution",
                "target_agent":     "null",
                "message_to_agent": "The system encountered an error. Please try again.",
                "messages":         [AIMessage("The system encountered an error. Please try again.")],
                "last_call_stat":   None,
            }

        content = raw_response.content
        raw = None

        if not content and raw_response.tool_calls:
            # Model put JSON in tool_calls instead of content
            tool_args = raw_response.tool_calls[0].get("args", {})
            print(f"[WARN] Model used tool_calls instead of content. Extracting args.")
            raw = {
                "thought":      str(tool_args.get("thought", "")),
                "action":       str(tool_args.get("action", "")),
                "target_agent": str(tool_args.get("target_agent", "null")),
                "message":      str(tool_args.get("message", "")),
            }

        if raw is None and not content:
            content = (
                raw_response.additional_kwargs.get("reasoning_content")
                or raw_response.additional_kwargs.get("thinking")
                or raw_response.additional_kwargs.get("response")
                or ""
            )
            if not content:
                print(f"[WARN] Empty content, additional_kwargs: {raw_response.additional_kwargs}")
                raise ValueError("LLM returned empty content")

        if raw is None:
            try:
                raw = _parse_json(content)
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(f"Failed to parse JSON: {e}\nContent: {content[:500]}")

        usage = _extract_token_usage(raw_response)
        call_stat = {
            "node":              "orchestrator",
            "prompt_tokens":     usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens":      usage["total_tokens"],
            "duration_ms":       duration_ms,
            "ttft_ms":           ttft_ms,
        }

        return {
            "thought":          str(raw.get("thought", "")),
            "action":           str(raw.get("action", "")),
            "target_agent":     str(raw.get("target_agent", "null")),
            "message_to_agent": str(raw.get("message", "")),
            "messages":         [AIMessage(str(raw.get("message", "")))],
            "last_call_stat":   call_stat,
        }
