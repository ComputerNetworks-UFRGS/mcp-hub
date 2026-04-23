from state import GraphState, RouterChoice
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel
from typing import Literal
from langchain_openai import ChatOpenAI
from langgraph.graph import END
from langgraph.types import Command




class RouterAgent:
        
    @staticmethod
    def initial_route(state: GraphState) -> str:
        return state["router_choice"]
    
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm.with_structured_output(RouterChoice)

        # self.prompt = ChatPromptTemplate.from_messages([
        #     ("system",
        #      "You are a Router Agent. Choose the agent that may better answer the user's questions.\n"
        #      "k8s_agent: can look at logs and other kubernetes resources.\n"
        #      "otel_agent: can look at opentelemetry traces.\n"
        #      "answer_agent: can answer generic questions. If you can't choose an agent, choose this and especify a reason."
        #      "If you can choose an appropriate agent, don't specify a reason, answer with \"\""),
        #     ("user", "{question}")
        # ])

        self.system = SystemMessage(content=
            "You are a Router Agent. Choose the agent that may better answer the user's questions.\n"
            "k8s_agent: can look at logs and other kubernetes resources.\n"
            "otel_agent: can look at opentelemetry traces.\n"
            "answer_agent: can answer generic questions. If you can't choose an agent, choose this and specify a reason.\n"
            "If you can choose an appropriate agent, don't specify a reason, answer with \"\"."
        )


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):
        question = state.get("question", "")


        # messages = [self.system, HumanMessage(content=user_content)]
        messages = [self.system] + state["messages"]

        # raw = await (self.prompt | self.llm).ainvoke({"question": question}, config=config)
        raw = await self.llm.ainvoke(messages, config=config)


        if raw is None or raw.agent is None: # type: ignore
            print(f"[RouterAgent] structured output falhou para: '{question}' | raw={raw}")
            return {"router_choice": "answer_agent", "router_reason": ""}
            # return {"router_choice": "router_error", "router_reason": ""}
    
        router_choice = raw.agent # type: ignore
        router_reason = raw.reason # type: ignore
        return {
            "router_choice": router_choice,
            "router_reason": router_reason
        }