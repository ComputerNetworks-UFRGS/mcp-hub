from state import GraphState
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from pydantic import BaseModel
from typing import Literal



class SingleAgent:
    def __init__(self, llm, tools):
        self.llm = llm.bind_tools(tools)
        
        self.system = SystemMessage(content=
                                    "You are a helpful AI assistant. Your job is to formulate a final answer to the user.\n"
            "You will receive the user's question and possibly responses from specialized agents:\n"
            "- k8s_agent: a Kubernetes specialist that can query logs and cluster resources.\n"
            "- otel_agent: an OpenTelemetry specialist that can analyze distributed traces.\n"
            "If any agent provided a response, summarize or repeat it clearly to the user.\n"
            "If no agent responded, answer based on your own knowledge.")


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):

        messages = [self.system] + state["messages"]
        raw = await self.llm.ainvoke(messages, config=config)
        return {
            "messages": [raw] ,
        }
        
    