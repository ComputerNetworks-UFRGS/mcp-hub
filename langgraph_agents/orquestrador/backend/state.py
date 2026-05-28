from typing import TypedDict, List, Optional, Annotated, Literal
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel


class GraphState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]

    thought: Optional[str]
    action: Optional[str]
    target_agent: Optional[str]
    message_to_agent: Optional[str]

    kubernetes_tool_history: Annotated[List[BaseMessage], add_messages]
    kubernetes_answer: Optional[BaseMessage]
    traces_tool_history: Annotated[List[BaseMessage], add_messages]
    traces_answer: Optional[BaseMessage]
    logs_tool_history: Annotated[List[BaseMessage], add_messages]
    logs_answer: Optional[BaseMessage]
    metrics_tool_history: Annotated[List[BaseMessage], add_messages]
    metrics_answer: Optional[BaseMessage]
    
