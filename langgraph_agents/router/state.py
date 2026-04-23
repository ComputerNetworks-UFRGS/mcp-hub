from typing import TypedDict, List, Optional, Annotated, Literal
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

class RouterChoice(BaseModel):
    """Agent to continue conversation."""
    agent: Literal["k8s_agent", "otel_agent", "answer_agent", "router_error"]
    reason: str

class GraphState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    k8s_tool_history: Annotated[List[BaseMessage], add_messages]
    otel_tool_history: Annotated[List[BaseMessage], add_messages]
    
    question: str 
    k8s_answer: Optional[str]
    otel_answer: Optional[str]
    final_answer: Optional[str]
  
    # routing
    router_choice: Literal["k8s_agent", "otel_agent", "answer_agent", "router_error"]
    router_reason: Optional[str]
