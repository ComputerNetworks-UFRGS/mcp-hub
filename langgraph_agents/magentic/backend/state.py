from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class GraphState(TypedDict):
    # ── Conversation with the user ────────────────────────────────────────────
    messages: Annotated[list, add_messages]

    # ── Magentic ledgers (free text, updated by orchestrator) ─────────────────
    task_ledger: str      # investigation plan created on the first call
    progress_ledger: str  # what has been found and what remains

    # ── Orchestrator decision ─────────────────────────────────────────────────
    action: str          # "dispatch" | "final_answer"
    next_agent: str      # agent to call next
    task_for_agent: str  # specific instruction for the agent
    last_agent: str      # agent that just finished (so orchestrator knows)

    # ── Final answer ──────────────────────────────────────────────────────────
    final_answer: str

    # ── Session ledger (persists across questions within a conversation) ──────
    session_ledger: str

    # ── Stall detection ───────────────────────────────────────────────────────
    iteration_count: int

    # ── Per-agent tool-call histories (consumed by LangGraph ToolNode) ────────
    kubernetes_tool_history: Annotated[list, add_messages]
    traces_tool_history:     Annotated[list, add_messages]
    logs_tool_history:       Annotated[list, add_messages]
    metrics_tool_history:    Annotated[list, add_messages]

    # ── Per-agent text answers ────────────────────────────────────────────────
    kubernetes_answer: str
    traces_answer:     str
    logs_answer:       str
    metrics_answer:    str

    # ── LLM call stats (overwritten on every LLM call, read by app.py for SSE) ─
    last_call_stat: Optional[dict]
    # shape: {node, prompt_tokens, completion_tokens, total_tokens, duration_ms}
