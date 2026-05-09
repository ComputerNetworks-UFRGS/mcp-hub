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
                                    "You are a helpful AI assistant. " \
                                    "Use your tools to answer the user's prompt." \
                                    "Don't ask the user to execute commands - you can use your tools.")


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):

        messages = [self.system] + state["messages"]
        raw = await self.llm.ainvoke(messages, config=config)
        return {
            "messages": [raw] ,
        }
        
    