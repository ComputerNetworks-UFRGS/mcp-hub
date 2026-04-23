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
from router_agent import RouterAgent
from answer_agent import AnswerAgent
from k8s_agent import K8sAgent
from otel_agent import OtelAgent

load_dotenv()

model = ChatOpenAI(
    model=str(os.getenv('MODELO_OPEN_WEB_UI', "gpt-oss:20b")),
    base_url=str(os.getenv('OLLAMA_BASE_URL', "http://localhost:1234")),
    temperature=0
)

async def get_mcp_tools(client : MultiServerMCPClient):
    tools = await client.get_tools()
    return tools


router_agent = RouterAgent(llm=model)
answer_agent = AnswerAgent(llm=model)

def router_error(state: GraphState):
    """Erro no roteamento."""
    print("Erro no roteamento.")
    print(state)
    response = "Erro no roteamento"
    return {"messages": [HumanMessage(response)]}


async def build_graph():
    k8s_mcp_client = MultiServerMCPClient(
        {
            "k8s": {
                "transport": "http", 
                "url": os.getenv('MCP_K8S_URL', "http://localhost:49447/mcp"),
            }
        } # type: ignore
    )
    otel_mcp_client = MultiServerMCPClient(
        {
            "jaeger": {
                "transport": "http", 
                "url": os.getenv("MCP_JAEGER_URL", "http://localhost:58191/mcp")
            }
        } # type: ignore
    )
    k8s_tool_list = await get_mcp_tools(k8s_mcp_client)
    otel_tool_list = await get_mcp_tools(otel_mcp_client)

    otel_agent = OtelAgent(llm=model, tools=otel_tool_list)
    k8s_agent = K8sAgent(llm=model, tools=k8s_tool_list)


    k8s_tool_node = ToolNode(tools=k8s_tool_list, messages_key="k8s_tool_history")
    otel_tool_node    = ToolNode(tools=otel_tool_list, messages_key="otel_tool_history")


    graph = StateGraph(GraphState)

    graph.add_node("router_agent", router_agent)
    graph.add_node("k8s_agent", k8s_agent)
    graph.add_node("otel_agent",    otel_agent)
    graph.add_node("answer_agent", answer_agent)
    graph.add_node("k8s_tool_node",  k8s_tool_node)
    graph.add_node("otel_tool_node",     otel_tool_node)
    graph.add_node("router_error",     router_error)

    # Router agent 
    graph.add_edge(START, "router_agent") 
    # graph.add_edge(START, "k8s_agent") 
    # graph.add_edge(START, "otel_agent") 
    # Choose route
    graph.add_conditional_edges(
        "router_agent",
        RouterAgent.initial_route,
        {"k8s_agent": "k8s_agent", 
        "otel_agent": "otel_agent", 
        "answer_agent": "answer_agent",
        "router_error": "router_error"}
    )

    # Route 1: k8s_agent
    graph.add_conditional_edges(
        "k8s_agent",
        partial(tools_condition, messages_key="k8s_tool_history"),
        {"tools": "k8s_tool_node", END: "answer_agent"})
        # {"tools": "k8s_tool_node", END: END})
    graph.add_edge("k8s_tool_node", "k8s_agent")

    # Route 2: k8s_agent
    graph.add_conditional_edges(
        "otel_agent",
        partial(tools_condition, messages_key="otel_tool_history"),
        {"tools": "otel_tool_node", END: "answer_agent"})
        # {"tools": "otel_tool_node", END: END})        
    graph.add_edge("otel_tool_node", "otel_agent")

    # Route 3: answer_agent
    # Also called after other routes
    graph.add_edge("answer_agent", END)

    # Caso de erro
    graph.add_edge("router_error", END)

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
            {"messages": [HumanMessage(content=prompt)],
             "question": prompt,
             "router_choice": "router_error",
             "k8s_tool_history": [HumanMessage(content=prompt)],
             "otel_tool_history": [HumanMessage(content=prompt)]
             },  # type: ignore
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
    # visualize_workflow()
    asyncio.run(main())