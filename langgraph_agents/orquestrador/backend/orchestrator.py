import random
import json

from state import GraphState
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from pydantic import BaseModel
from typing import Literal
from langchain_openai import ChatOpenAI
from langgraph.graph import END
from langgraph.types import Command




class Orchestrator:
        
    @staticmethod
    def choose_action(state: GraphState) -> str:
        if state["action"] == "delegate_to_agent":
            if state["target_agent"] in ["metrics_agent", "traces_agent", "logs_agent", "kubernetes_agent"]:
                return state["target_agent"]
        
        # if state.get("message_to_agent",""):
        #     state["messages"].append(AIMessage(state["message_to_agent"]))
        return "END"
    
    
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.parser = JsonOutputParser()

      
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


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):
        # messages = [self.system, HumanMessage(content=user_content)]
        messages = [self.system] + state["messages"]

        # print(messages)
        pre = await self.llm.ainvoke(messages, config=config, seed=random.randint(0, 99999),
)
        # print(pre)
        content = pre.content
        if not content and pre.tool_calls:
            # O modelo colocou o JSON em tool_calls em vez de content
            # Pega os args do primeiro tool_call e serializa de volta para JSON
            tool_args = pre.tool_calls[0].get("args", {})
            print(f"[WARN] Modelo usou tool_calls em vez de content. Extraindo args.")
            print(tool_args)
            print({
                "thought": str(tool_args.get("thought", "")),
                "action": str(tool_args.get("action", "")),
                "target_agent": str(tool_args.get("target_agent", "null")),
                "message_to_agent": str(tool_args.get("message", "")),
                "messages": [AIMessage(str(tool_args.get("message", "")))]
            })
            return {
                "thought": str(tool_args.get("thought", "")),
                "action": str(tool_args.get("action", "")),
                "target_agent": str(tool_args.get("target_agent", "null")),
                "message_to_agent": str(tool_args.get("message", "")),
                "messages": [AIMessage(str(tool_args.get("message", "")))]
            }

        if not content:
            content = (
                pre.additional_kwargs.get("reasoning_content")
                or pre.additional_kwargs.get("thinking")
                or pre.additional_kwargs.get("response")
                or ""
            )
            print(f"[WARN] content vazio, additional_kwargs: {pre.additional_kwargs}")
            print()
            print()
            print(messages)
            print()
            print()
            print(pre)
        

            raise ValueError("LLM retornou conteúdo vazio")

        raw = await self.parser.ainvoke(pre, config=config)
        # raw = await (self.llm | self.parser).ainvoke(messages, config=config)
        # print(raw)
        return {
            "thought": str(raw.get("thought", "")),
            "action": str(raw.get("action", "")),
            "target_agent": str(raw.get("target_agent", "null")),
            "message_to_agent": str(raw.get("message", "")),
            "messages": [AIMessage(str(raw.get("message", "")))]
        }
