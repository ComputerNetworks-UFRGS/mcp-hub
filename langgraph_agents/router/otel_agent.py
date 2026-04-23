from state import GraphState
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from pydantic import BaseModel
from typing import Literal



class OtelAgent:
    def __init__(self, llm, tools):
        self.llm = llm.bind_tools(tools)

        self.system = SystemMessage(content="""You are a Site Reliability Engineer (SRE) specialized in observability and OpenTelemetry.
            Your task is to analyze distributed traces via Jaeger to find performance bottlenecks and errors.
            """)
            # Rules:
            # 1. If the user does not specify a service, use get_services() to discover the available ones.
            # 2. If you need to investigate slowness, use search_traces with the 'minDuration' parameter (e.g.: '500ms', '2s').
            # 3. If you need to investigate failures, use search_traces passing tags='{"error":"true"}'.
            # 4. Whenever you find a suspicious trace in the listing, use get_trace_details passing the TraceID to see the complete execution flow (spans).
            # 5. Base your answers strictly on what was returned by the tools. Never fabricate trace IDs, durations, or service names.
            # """)


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):
        messages = [self.system] + state["otel_tool_history"]
        raw = await self.llm.ainvoke(messages, config=config)
        return {
            "otel_tool_history": [raw] ,
            "otel_answer": raw.content
        }
    
    