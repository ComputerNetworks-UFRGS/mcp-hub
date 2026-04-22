from state import GraphState
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from pydantic import BaseModel
from typing import Literal



class K8sAgent:
    def __init__(self, llm, tools):
        self.llm = llm.bind_tools(tools)

        self.system = SystemMessage(content="""You are an SRE resolving incidents in Kubernetes. Never ask the user to run commands. Investigate on your own.""")
            # 1. NEVER guess pod names. Always call get_pods first.
            # 2. Discover the correct namespace with get_namespaces.
            # 3. Check the pods. Specify the correct container when fetching logs.
            # 4. LOG STRATEGY: When using search_pod_logs, results will have matching lines marked as '>> MATCH:'. Read ONLY what is explicitly in the marked lines or their immediate context. NEVER fabricate logs, numbers, or success tables that were not requested. Focus on reporting only the errors/matches found.
            # 5. Never ask the user to run commands. Investigate on your own.""")


    async def __call__(self, state: GraphState, config: RunnableConfig|None = None):
        messages = [self.system] + state["k8s_tool_history"]
        raw = await self.llm.ainvoke(messages, config=config)
        return {
            "k8s_tool_history": [raw] ,
            "k8s_answer": raw.content
        }
    
    
    