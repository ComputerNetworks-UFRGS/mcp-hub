import asyncio
import os
from functools import partial
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, RemoveMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from state import GraphState
from orchestrator import MagenticOrchestrator
from agents import make_agent

load_dotenv()


def get_model() -> ChatOpenAI:
    return ChatOpenAI(
        model=str(os.getenv("MODELO_OPEN_WEB_UI", "gpt-oss:20b")),
        base_url=str(os.getenv("OLLAMA_BASE_URL", "http://localhost:1234")),
        temperature=0.0,
    )


def reset_ledger(state: GraphState) -> dict:
    """
    Resets all investigation state at the start of each new user message.
    Plain fields are overwritten directly; tool-history lists (add_messages)
    require RemoveMessage to be properly cleared.
    """
    update: dict = {
        "task_ledger":       "",
        "progress_ledger":   "",
        "action":            "",
        "next_agent":        "",
        "task_for_agent":    "",
        "last_agent":        "",
        "iteration_count":   0,
        "final_answer":      "",
        "kubernetes_answer": "",
        "traces_answer":     "",
        "logs_answer":       "",
        "metrics_answer":    "",
    }

    # RemoveMessage is the only way to clear add_messages-annotated lists
    for name in ("kubernetes", "traces", "logs", "metrics"):
        key = f"{name}_tool_history"
        history = state.get(key) or []
        if history:
            update[key] = [RemoveMessage(id=m.id) for m in history]

    return update


async def build_graph():
    model = get_model()

    # ── Connect to MCP servers ────────────────────────────────────────────────
    clients = {
        "kubernetes": MultiServerMCPClient({"k8s": {
            "transport": "http",
            "url": os.getenv("MCP_K8S_OPERATIONS_URL", "http://localhost:49447/mcp"),
        }}),  # type: ignore
        "traces": MultiServerMCPClient({"traces": {
            "transport": "http",
            "url": os.getenv("MCP_JAEGER_URL", "http://localhost:58191/mcp"),
        }}),  # type: ignore
        "logs": MultiServerMCPClient({"logs": {
            "transport": "http",
            "url": os.getenv("MCP_K8S_LOGS_URL", "http://localhost:494/mcp"),
        }}),  # type: ignore
        "metrics": MultiServerMCPClient({"metrics": {
            "transport": "sse",
            "url": os.getenv("MCP_PROMETHEUS_URL", "http://localhost:52414/sse"),
        }}),  # type: ignore
    }

    tools = {name: await client.get_tools() for name, client in clients.items()}

    # ── Build graph ───────────────────────────────────────────────────────────
    orchestrator = MagenticOrchestrator(llm=model)

    graph = StateGraph(GraphState)
    graph.add_node("reset_ledger", reset_ledger)
    graph.add_node("orchestrator", orchestrator)

    for name, agent_tools in tools.items():
        graph.add_node(f"{name}_agent",     make_agent(name, model, agent_tools))
        graph.add_node(f"{name}_tool_node", ToolNode(
            tools=agent_tools,
            messages_key=f"{name}_tool_history",
        ))

        # tool loop: agent → tool_node → agent (until no tool calls)
        graph.add_conditional_edges(
            f"{name}_agent",
            partial(tools_condition, messages_key=f"{name}_tool_history"),
            {"tools": f"{name}_tool_node", END: "orchestrator"},
        )
        graph.add_edge(f"{name}_tool_node", f"{name}_agent")

    # Entry: always reset before orchestrator
    graph.add_edge(START, "reset_ledger")
    graph.add_edge("reset_ledger", "orchestrator")

    # Orchestrator routes to agents or END
    graph.add_conditional_edges(
        "orchestrator",
        MagenticOrchestrator.route,
        {
            "kubernetes_agent": "kubernetes_agent",
            "traces_agent":     "traces_agent",
            "logs_agent":       "logs_agent",
            "metrics_agent":    "metrics_agent",
            "END":              END,
        },
    )

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main():
    app = await build_graph()
    thread_id = "magentic-1"
    print("Magentic system ready. Type 'exit' to quit.\n")

    while True:
        prompt = input("You: ").strip()
        if prompt.lower() in ("exit", "quit", "sair"):
            break

        config = {"configurable": {"thread_id": thread_id}}
        print()

        async for chunk in app.astream(
            {"messages": [HumanMessage(content=prompt)]},
            config,
            stream_mode="updates",
        ):
            for node_name, update in chunk.items():

                if node_name == "reset_ledger":
                    continue

                def _s(val) -> str:
                    if isinstance(val, list):
                        return "\n".join(str(x) for x in val)
                    return str(val) if val is not None else ""

                # ── Orchestrator updates ──────────────────────────────────────
                if node_name == "orchestrator":
                    task_ledger = _s(update.get("task_ledger"))
                    if task_ledger:
                        print("╔══ TASK LEDGER " + "═" * 45)
                        for line in task_ledger.strip().splitlines():
                            print(f"║  {line}")
                        print("╚" + "═" * 60 + "\n")

                    progress = _s(update.get("progress_ledger"))
                    if progress:
                        print("┌── PROGRESS LEDGER " + "─" * 41)
                        for line in progress.strip().splitlines():
                            print(f"│  {line}")
                        print("└" + "─" * 60 + "\n")

                    if update.get("action") == "dispatch":
                        agent = update.get("next_agent", "?")
                        task  = _s(update.get("task_for_agent"))
                        print(f"  ▶ Dispatching to [{agent.upper()}]")
                        print(f"    Task: {task}\n")

                    final = _s(update.get("final_answer"))
                    if final:
                        print("\n" + "═" * 60)
                        print("FINAL ANSWER:")
                        print(final)
                        print("═" * 60 + "\n")

                # ── Agent node: show tool calls it requested ───────────────────
                elif node_name.endswith("_agent"):
                    agent_label = node_name.replace("_agent", "").upper()
                    hist_key    = node_name.replace("_agent", "_tool_history")
                    for msg in (update.get(hist_key) or []):
                        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                            for tc in msg.tool_calls:
                                print(f"    [{agent_label}] → {tc.get('name', '')}({tc.get('args', {})})")

                    # Final response from this agent
                    answer_key = node_name.replace("_agent", "_answer")
                    answer = _s(update.get(answer_key))
                    if answer:
                        print(f"\n  ◀ [{agent_label}] responded:")
                        print(f"    {answer.strip()}\n")

                # ── ToolNode: show tool results ────────────────────────────────
                elif node_name.endswith("_tool_node"):
                    agent_label = node_name.replace("_tool_node", "").upper()
                    hist_key    = node_name.replace("_tool_node", "_tool_history")
                    for msg in (update.get(hist_key) or []):
                        if isinstance(msg, ToolMessage):
                            content = msg.content
                            if len(content) > 300:
                                content = content[:300] + "…"
                            print(f"    [{agent_label}] ← {msg.name}: {content}")


if __name__ == "__main__":
    asyncio.run(main())
