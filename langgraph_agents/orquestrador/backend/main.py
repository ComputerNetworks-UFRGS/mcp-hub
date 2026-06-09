from typing import Annotated, Literal
from typing_extensions import TypedDict
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command
from langchain_core.messages import HumanMessage
import asyncio
from langfuse.langchain import CallbackHandler
import os
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langchain_mcp_adapters.client import MultiServerMCPClient  
from functools import partial

from state import GraphState
from orchestrator import Orchestrator
from kubernetes_agent import KubernetesAgent
from traces_agent import TracesAgent
from logs_agent import LogsAgent
from metrics_agent import MetricsAgent

load_dotenv()

model = ChatOpenAI(
    model=str(os.getenv('MODELO_OPEN_WEB_UI', "gpt-oss:20b")),
    base_url=str(os.getenv('OLLAMA_BASE_URL', "http://localhost:1234")),
    temperature=0.0,
    # Ask the server to include token usage in the final streaming chunk.
    # Ollama ≥ 0.5 and any OpenAI-compatible server that supports the
    # stream_options extension will return usage counts.
    stream_usage=True,
)

async def get_mcp_tools(client : MultiServerMCPClient):
    tools = await client.get_tools()
    return tools


def add_final_agent_message(state: GraphState):
    if state["target_agent"] not in ["kubernetes_agent", "traces_agent", "logs_agent", "metrics_agent"]:
        return {}
    
    agent_name = state["target_agent"].removesuffix("_agent")
    answer = state.get(f"{agent_name}_answer")
    
    if not answer or not answer.content:
        return {}
    
    return {
        "messages": [HumanMessage(content=f"[{state['target_agent']} report]\n{answer.content}")]
    }



async def build_graph():
    orchestrator = Orchestrator(llm=model)

    kubernetes_mcp_client = MultiServerMCPClient(
        {
            "k8s": {
                "transport": "http", 
                "url": os.getenv('MCP_K8S_OPERATIONS_URL', "http://localhost:49447/mcp"),
            }
        } # type: ignore
    )
    traces_mcp_client = MultiServerMCPClient(
        {
            "traces": {
                "transport": "http", 
                "url": os.getenv("MCP_JAEGER_URL", "http://localhost:58191/mcp")
            }
        } # type: ignore
    )
    logs_mcp_client = MultiServerMCPClient(
        {
            "logs": {
                "transport": "http", 
                "url": os.getenv('MCP_K8S_LOGS_URL', "http://localhost:494/mcp"),
            }
        } # type: ignore
    )
    metrics_mcp_client = MultiServerMCPClient(
        {
            "metrics": {
                "transport": "sse", 
                "url": os.getenv('MCP_PROMETHEUS_URL', "http://localhost:52414/sse"),
            }
        } # type: ignore
    )

    kubernetes_tool_list = await get_mcp_tools(kubernetes_mcp_client)
    traces_tool_list = await get_mcp_tools(traces_mcp_client)
    logs_tool_list = await get_mcp_tools(logs_mcp_client)
    metrics_tool_list = await get_mcp_tools(metrics_mcp_client)
    

    kubernetes_agent = KubernetesAgent(llm=model, tools=kubernetes_tool_list)
    traces_agent = TracesAgent(llm=model, tools=traces_tool_list)
    logs_agent = LogsAgent(llm=model, tools=logs_tool_list)
    metrics_agent = MetricsAgent(llm=model, tools=metrics_tool_list)

    kubernetes_tool_node = ToolNode(tools=kubernetes_tool_list, messages_key="kubernetes_tool_history")
    traces_tool_node = ToolNode(tools=traces_tool_list, messages_key="traces_tool_history")
    logs_tool_node = ToolNode(tools=logs_tool_list, messages_key="logs_tool_history")
    metrics_tool_node = ToolNode(tools=metrics_tool_list, messages_key="metrics_tool_history")


    graph = StateGraph(GraphState)

    graph.add_node("orchestrator", orchestrator)
    graph.add_node("kubernetes_agent", kubernetes_agent)
    graph.add_node("kubernetes_tool_node",  kubernetes_tool_node)
    graph.add_node("traces_agent",    traces_agent)
    graph.add_node("traces_tool_node",     traces_tool_node)
    graph.add_node("logs_agent",    logs_agent)
    graph.add_node("logs_tool_node",     logs_tool_node)
    graph.add_node("metrics_agent",    metrics_agent)
    graph.add_node("metrics_tool_node",     metrics_tool_node)
    graph.add_node("add_final_agent_message",     add_final_agent_message)

    # Orchestrator 
    graph.add_edge(START, "orchestrator") 
    # graph.add_edge("orchestrator", END) 

    graph.add_conditional_edges(
        "orchestrator", 
        Orchestrator.choose_action,
        {
            "metrics_agent": "metrics_agent", 
            "traces_agent": "traces_agent", 
            "logs_agent": "logs_agent", 
            "kubernetes_agent": "kubernetes_agent",
            "END": END
        }        
    )
    

    # Loop para uso de tools
    for agent_name in ["kubernetes", "traces", "logs", "metrics"]:
        graph.add_conditional_edges(
            f"{agent_name}_agent",
            partial(tools_condition, messages_key=f"{agent_name}_tool_history"),
            {"tools": f"{agent_name}_tool_node", END: "add_final_agent_message"})
        graph.add_edge(f"{agent_name}_tool_node", f"{agent_name}_agent")
    
    graph.add_edge("add_final_agent_message", "orchestrator")




    memory = MemorySaver()

    app = graph.compile(checkpointer=memory)
    return app


# ── MAIN ──────────────────────────────────────────────────────────────────

# # Suprimindo um warning do pydantic 
# import warnings
# warnings.filterwarnings(
#     "ignore",
#     message="Pydantic serializer warnings",
#     category=UserWarning,
#     module="pydantic"
# )

from datetime import datetime, timezone, timedelta
from uuid import uuid4

async def main():
    app = await build_graph()
    script_filename = os.path.basename(__file__)
    tz_minus_3 = timezone(timedelta(hours=-3))
    session_id = str(datetime.now(tz_minus_3))
    print("Digite 'sair' para encerrar.")
    while True:
        prompt = input("Você: ").strip()
        if prompt.lower() == "sair":
            break

        trace_name=uuid4().hex[:32]
        langfuse_handler = CallbackHandler(trace_context={"trace_id":trace_name})
        thread_id = "conversa-1"  # identifica a sessão

        config = {
            "callbacks": [langfuse_handler],
            "metadata": {"langfuse_session_id": session_id},
            "configurable": {"thread_id": thread_id}
        }
        result = None
        async for chunk in app.astream(
            {"messages": [HumanMessage(content=prompt)]},  # type: ignore
            config, # type: ignore
            stream_mode="updates"  # retorna só o que cada nodo alterou no estado
        ):
            for node_name, state_update in chunk.items():
                print(f"\n── {node_name} ──")
                print(state_update)
                if "messages" in state_update:
                    result = state_update["messages"][-1].content



        print(f"\nAgente: {result}")
    
    print("==========FINAL==========")
    print(app.get_state(config=config).values) # type: ignore




async def visualize_workflow():
    app = await build_graph()
    try:
        graph_png = app.get_graph().draw_mermaid_png()
        with open("workflow_graph.png", "wb") as f:
            f.write(graph_png)
    except Exception as e:
        print(f"Could not visualize workflow: {e}")


if __name__ == "__main__":
    # asyncio.run(visualize_workflow())
    asyncio.run(main())