from state import GraphState
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from pydantic import BaseModel
from typing import Literal



class ValidatorAgent:
    def __init__(self, llm):
        self.llm = llm
        
        self.system = SystemMessage(content=
                                    "You are a validator AI agent." \
                                    "The user will ask the main agent a question. Your job is to decide if the given response answers the user's original prompt appropriately." \
                                    "If it does, answer with 'GOOD ANSWER'." \
                                    "If it does not, give feedback to the agent in order to generate an better answer. Make it clear you are a validator answer." \
                                    "DO NOT ANSWER THE USER'S PROMPT. ONLY ANSWER WITH 'GOOD ANSWER' OR WITH FEEDBACK.")


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):

        messages = [self.system] + state["messages"]
        raw = await self.llm.ainvoke(messages, config=config)
        return {
            "messages": [raw] ,
        }
        
    