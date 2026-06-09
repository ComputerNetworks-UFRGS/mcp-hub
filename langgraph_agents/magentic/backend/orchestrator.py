import asyncio
import json
import re
import time
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, AIMessageChunk
from state import GraphState
from agents import _extract_token_usage

MAX_ITERATIONS = 10
MAX_LLM_RETRIES = 2   # extra retries after first attempt (total = 3)

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


# Short fallback prompt used on the last retry attempt when the normal prompt fails.
# Much shorter than PROMPT_PHASE1/PHASE2 — avoids context-length issues.
PROMPT_FALLBACK = """\
The investigation could not be completed, but some data was collected.
Summarise the findings below into a final answer for the user.

Progress collected:
{progress}

Respond ONLY with this JSON (no other text):
{{"action": "final_answer", "progress_ledger": "see progress above", "final_answer": "<your summary>"}}"""


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

    async def _invoke(self, messages: list, fallback_messages: list) -> tuple:
        """
        Invoke the LLM with retry + fallback, using streaming to capture TTFT.

        - Attempts 1..MAX_LLM_RETRIES: use the original messages.
        - Final attempt: switch to the shorter fallback_messages to avoid
          context-length failures.
        - Between attempts: brief exponential backoff (0.5s, 1s, …).

        Returns (response_or_None, error_msg_or_None, duration_ms, ttft_ms_or_None).
        duration_ms: total call time (excluding sleep).
        ttft_ms:     time from call start until first token arrived (provider latency).
        """
        last_error = None
        total = MAX_LLM_RETRIES + 1
        for attempt in range(total):
            is_last = attempt == total - 1
            msgs = fallback_messages if is_last else messages
            t0 = time.monotonic()
            t_first: float | None = None
            try:
                chunks = []
                async for chunk in self.llm.astream(msgs):
                    if t_first is None:
                        t_first = time.monotonic()
                    chunks.append(chunk)
                if chunks:
                    merged: AIMessageChunk = chunks[0]
                    for c in chunks[1:]:
                        merged = merged + c
                    # Convert to AIMessage for full compatibility
                    resp = AIMessage(
                        content=merged.content,
                        tool_calls=list(getattr(merged, "tool_calls", []) or []),
                        usage_metadata=getattr(merged, "usage_metadata", None),
                        response_metadata=getattr(merged, "response_metadata", {}),
                        id=getattr(merged, "id", None),
                    )
                    dur  = int((time.monotonic() - t0) * 1000)
                    ttft = int((t_first - t0) * 1000) if t_first is not None else None
                    return resp, None, dur, ttft
                last_error = "model returned empty stream"
            except Exception as e:
                last_error = str(e)
            if not is_last:
                await asyncio.sleep(0.5 * (attempt + 1))
        return None, last_error, 0, None

    def _fallback_messages(self, state: GraphState) -> list:
        """Build a minimal prompt asking the LLM to summarise what was found so far."""
        progress = (state.get("progress_ledger") or "").strip()
        prompt = PROMPT_FALLBACK.format(progress=progress or "(nothing collected yet)")
        return [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]

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
        response, invoke_error, duration_ms, ttft_ms = await self._invoke(messages, self._fallback_messages(state))

        if response is None:
            # All retries and the fallback prompt failed — surface what was collected
            progress = (state.get("progress_ledger") or "").strip()
            error_msg = (
                f"Orchestrator failed after {MAX_LLM_RETRIES + 1} attempts "
                f"({invoke_error or 'no response'})."
            )
            final = f"{error_msg}\n\nPartial findings:\n{progress}" if progress else error_msg
            return {
                "action": "final_answer",
                "final_answer": final,
                "messages": [AIMessage(content=final)],
                "iteration_count": iteration,
            }

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

        usage  = _extract_token_usage(response)
        update: dict = {
            "iteration_count": iteration,
            "last_call_stat": {
                "node":               "orchestrator",
                "prompt_tokens":      usage["prompt_tokens"],
                "completion_tokens":  usage["completion_tokens"],
                "total_tokens":       usage["total_tokens"],
                "duration_ms":        duration_ms,
                "ttft_ms":            ttft_ms,   # time to first token (provider latency)
            },
        }

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
