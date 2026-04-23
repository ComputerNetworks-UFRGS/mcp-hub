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
from single_agent import SingleAgent
load_dotenv()

model = ChatOpenAI(
    model=str(os.getenv('MODELO_OPEN_WEB_UI', "gpt-oss:20b")),
    base_url=str(os.getenv('OLLAMA_BASE_URL', "http://localhost:1234")),
    temperature=0
)



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

    tool_list = await k8s_mcp_client.get_tools() + await otel_mcp_client.get_tools()

    single_agent = SingleAgent(llm=model, tools=tool_list)
    
    tool_node = ToolNode(tools=tool_list)

    graph = StateGraph(GraphState)

    graph.add_node("single_agent",    single_agent)
    graph.add_node("tool_node",     tool_node)

    graph.add_edge(START, "single_agent") 
    
    graph.add_conditional_edges(
        "single_agent",
        tools_condition,
        {"tools": "tool_node", END: END})
    graph.add_edge("tool_node", "single_agent")

    graph.add_edge("single_agent", END)

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