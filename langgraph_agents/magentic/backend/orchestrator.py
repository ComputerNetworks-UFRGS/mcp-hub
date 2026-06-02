import json
import re
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from state import GraphState

MAX_ITERATIONS = 10

SYSTEM_PROMPT = """\
You are the orchestrator of a Kubernetes cluster diagnostic system.
You have access to 4 specialist agents:
  - kubernetes : state of pods, deployments, services, events and cluster resources
  - traces     : distributed request tracing via Jaeger
  - logs       : container logs via Kubernetes
  - metrics    : performance and availability metrics via Prometheus

Always respond ONLY with valid JSON. No text outside the JSON block."""

PROMPT_PHASE1 = """\
{history_section}{session_section}{knowledge_section}\
The user asked:
{question}

Create a task ledger describing:
1. What needs to be discovered to answer the question
2. Which agents are relevant and why
3. Suggested investigation order

Then select the first agent and write a specific task for it.

Respond with JSON:
{{
  "task_ledger": "<detailed investigation plan>",
  "next_agent": "<kubernetes|traces|logs|metrics>",
  "task_for_agent": "<specific instruction for the agent>"
}}"""

PROMPT_PHASE2 = """\
Task Ledger:
{task_ledger}

Progress Ledger:
{progress_ledger}

The '{last_agent}' agent just responded:
{last_agent_answer}

Update the progress ledger with these findings.
Then assess: is the task complete, or is there more to investigate?

If complete:
{{
  "action": "final_answer",
  "progress_ledger": "<updated ledger>",
  "final_answer": "<complete and clear response for the user>"
}}

If incomplete:
{{
  "action": "dispatch",
  "progress_ledger": "<updated ledger>",
  "next_agent": "<kubernetes|traces|logs|metrics>",
  "task_for_agent": "<specific instruction for the next agent>"
}}"""


HISTORY_TURNS = 3


def _format_history(messages: list, n_turns: int = HISTORY_TURNS) -> str:
    """
    Build a conversation-history string from the last n_turns of (Human, AI) pairs,
    excluding the current (last) message which is the question being handled now.
    Returns an empty string when there is no prior history.
    """
    prior = messages[:-1]  # everything except the current question

    turns: list[tuple[str, str]] = []
    i = 0
    while i < len(prior):
        if isinstance(prior[i], HumanMessage):
            user_text = prior[i].content
            if i + 1 < len(prior) and isinstance(prior[i + 1], AIMessage):
                turns.append((user_text, prior[i + 1].content))
                i += 2
                continue
        i += 1

    recent = turns[-n_turns:]
    if not recent:
        return ""

    lines = ["Previous conversation (last {} turn{}):".format(
        len(recent), "s" if len(recent) > 1 else ""
    )]
    for user_msg, ai_msg in recent:
        lines.append(f"  User: {user_msg}")
        lines.append(f"  Assistant: {ai_msg}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    """Extract JSON from an LLM response, tolerating markdown code fences."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    return json.loads(text)


class MagenticOrchestrator:
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, state: GraphState) -> dict:
        iteration = state.get("iteration_count", 0) + 1

        # Stall detection: force a final answer if the limit is exceeded
        if iteration > MAX_ITERATIONS:
            progress = state.get("progress_ledger", "Investigation incomplete.")
            return {
                "action": "final_answer",
                "final_answer": (
                    f"Reached the limit of {MAX_ITERATIONS} iterations.\n\n"
                    f"Progress so far:\n{progress}"
                ),
                "messages": [AIMessage(content=f"[max iterations reached]\n{progress}")],
                "iteration_count": iteration,
            }

        task_ledger = state.get("task_ledger", "")
        is_first_call = not task_ledger

        if is_first_call:
            user_message = state["messages"][-1].content
            history = _format_history(state.get("messages", []))
            history_section = history + "\n\n---\n\n" if history else ""

            session = (state.get("session_ledger") or "").strip()
            session_section = (
                f"Session Knowledge (facts from this conversation):\n{session}\n\n---\n\n"
                if session else ""
            )

            from reflect import ENABLE_KNOWLEDGE_BASE, _read_knowledge, _ensure_knowledge_file
            knowledge_section = ""
            if ENABLE_KNOWLEDGE_BASE:
                _ensure_knowledge_file()
                kb = _read_knowledge()
                if kb:
                    knowledge_section = f"Cluster Knowledge Base:\n{kb}\n\n---\n\n"

            prompt = PROMPT_PHASE1.format(
                question=user_message,
                history_section=history_section,
                session_section=session_section,
                knowledge_section=knowledge_section,
            )
        else:
            last_agent = state.get("last_agent", "")
            last_answer = state.get(f"{last_agent}_answer", "(no response)")
            prompt = PROMPT_PHASE2.format(
                task_ledger=task_ledger,
                progress_ledger=state.get("progress_ledger", "(empty)"),
                last_agent=last_agent,
                last_agent_answer=last_answer,
            )

        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]
        response = await self.llm.ainvoke(messages)

        try:
            data = _parse_json(response.content)
        except (json.JSONDecodeError, ValueError) as e:
            error_msg = (
                f"Internal error parsing orchestrator response: {e}\n\n"
                f"Raw response:\n{response.content}"
            )
            return {
                "action": "final_answer",
                "final_answer": error_msg,
                "messages": [AIMessage(content=error_msg)],
                "iteration_count": iteration,
            }

        update: dict = {"iteration_count": iteration}

        def _to_str(value, fallback: str = "") -> str:
            if isinstance(value, list):
                return "\n".join(str(item) for item in value)
            return str(value) if value is not None else fallback

        if is_first_call:
            update["task_ledger"]     = _to_str(data.get("task_ledger"))
            update["progress_ledger"] = ""
            update["action"]          = "dispatch"
            update["next_agent"]      = data.get("next_agent", "kubernetes")
            update["task_for_agent"]  = _to_str(data.get("task_for_agent"))
        else:
            update["progress_ledger"] = _to_str(
                data.get("progress_ledger"), state.get("progress_ledger", "")
            )
            update["action"]          = data.get("action", "final_answer")

            if update["action"] == "dispatch":
                update["next_agent"]     = data.get("next_agent", "kubernetes")
                update["task_for_agent"] = _to_str(data.get("task_for_agent"))
            else:
                final = _to_str(data.get("final_answer"))
                update["final_answer"] = final
                # Add final answer to conversation history for follow-up questions
                update["messages"] = [AIMessage(content=final)]

        return update

    @staticmethod
    def route(state: GraphState) -> str:
        """Conditional edge: where to go after the orchestrator."""
        if state.get("action") == "final_answer":
            return "reflect"
        agent = state.get("next_agent", "kubernetes")
        return f"{agent}_agent"
