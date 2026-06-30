import asyncio
import os
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langfuse.langchain import CallbackHandler

from state import GraphState
from orchestrator import Orchestrator
from agents import make_agent_tool

load_dotenv()

model = ChatOpenAI(
    model=str(os.getenv("MODELO_OPEN_WEB_UI", "gpt-oss:20b")),
    base_url=str(os.getenv("OLLAMA_BASE_URL", "http://localhost:1234")),
    temperature=0.0,
    stream_usage=True,
)

_GITHUB_ALLOWED = {
    "get_commit", "get_file_contents", "get_label", "get_latest_release",
    "get_release_by_tag", "get_tag", "issue_read", "issue_write",
    "list_branches", "list_commits", "list_issue_fields", "list_issue_types",
    "list_issues", "list_pull_requests", "list_releases", "list_tags",
    "pull_request_read", "search_code", "search_commits", "search_issues",
    "search_pull_requests", "search_repositories", "update_pull_request",
    "update_pull_request_branch",
}


async def build_graph():
    # ── Connect to MCP servers ────────────────────────────────────────────────
    mcp_configs = {
        # "kubernetes": {"k8s":     {"transport": "http", "url": os.getenv("MCP_K8S_OPERATIONS_URL", "http://localhost:49447/mcp")}},
        "traces":     {"traces":  {"transport": "http", "url": os.getenv("MCP_JAEGER_URL",          "http://localhost:58191/mcp")}},
        "logs":       {"logs":    {"transport": "http", "url": os.getenv("MCP_K8S_LOGS_URL",        "http://localhost:494/mcp")}},
        "metrics":    {"metrics": {"transport": "sse",  "url": os.getenv("MCP_PROMETHEUS_URL",      "http://localhost:52414/sse")}},
        "github":     {"github":  {
            "transport": "http",
            "url": os.getenv("MCP_GITHUB_URL", "https://api.githubcopilot.com/mcp"),
            "headers": {"Authorization": f'Bearer {os.getenv("MCP_GITHUB_KEY", "")}'},
        }},
    }

    mcp_tools: dict[str, list] = {}
    for name, cfg in mcp_configs.items():
        client = MultiServerMCPClient(cfg)  # type: ignore
        tools = await client.get_tools()
        if name == "github":
            tools = [t for t in tools if t.name in _GITHUB_ALLOWED]
        mcp_tools[name] = tools

    # ── Build agent tools (each sub-agent is now a single LangChain tool) ────
    agent_tools = [
        make_agent_tool(name, model, mcp_tools[name])
        for name in ("traces", "logs", "metrics", "github")
        # for name in ("kubernetes", "traces", "logs", "metrics", "github")
    ]

    # ── Build graph ───────────────────────────────────────────────────────────
    orchestrator = Orchestrator(llm=model, agent_tools=agent_tools)
    tool_node = ToolNode(tools=agent_tools, handle_tool_errors=True)

    graph = StateGraph(GraphState)
    graph.add_node("orchestrator", orchestrator)
    graph.add_node("tool_node", tool_node)

    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges(
        "orchestrator",
        tools_condition,
        {"tools": "tool_node", END: END},
    )
    graph.add_edge("tool_node", "orchestrator")

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main():
    app = await build_graph()
    tz_minus_3 = timezone(timedelta(hours=-3))
    session_id = str(datetime.now(tz_minus_3))
    thread_id = "conversa-1"
    print("Digite 'sair' para encerrar.")

    while True:
        prompt = input("Você: ").strip()
        if prompt.lower() == "sair":
            break

        langfuse_handler = CallbackHandler(trace_context={"trace_id": uuid4().hex[:32]})
        config = {
            "callbacks": [langfuse_handler],
            "metadata": {"langfuse_session_id": session_id},
            "configurable": {"thread_id": thread_id},
        }

        result = None
        async for chunk in app.astream(
            {"messages": [HumanMessage(content=prompt)]},
            config,
            stream_mode="updates",
        ):
            for node_name, state_update in chunk.items():
                print(f"\n── {node_name} ──")
                print(state_update)
                if "messages" in state_update:
                    result = state_update["messages"][-1].content

        print(f"\nAgente: {result}")


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
