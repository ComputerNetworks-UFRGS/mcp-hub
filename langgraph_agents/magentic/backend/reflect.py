"""
Post-investigation reflection node.

Feature flags (set to False to disable each ledger independently):
  ENABLE_SESSION_LEDGER  – accumulates facts within the current conversation
  ENABLE_KNOWLEDGE_BASE  – appends persistent insights to knowledge.md
"""

import os
import json
import re
import time
from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, AIMessageChunk
from state import GraphState
from agents import _extract_token_usage

# ── Feature flags ─────────────────────────────────────────────────────────────
ENABLE_SESSION_LEDGER = True
ENABLE_KNOWLEDGE_BASE = True

KNOWLEDGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.md")

# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a reflection assistant for a Kubernetes diagnostic system.
Your job is to extract useful knowledge from a completed investigation.
Always respond ONLY with valid JSON. No text outside the JSON block."""

_REFLECT_HEADER = """\
An investigation has just finished. Extract knowledge from it.

Task Ledger (what was planned):
{task_ledger}

Progress Ledger (what was found):
{progress_ledger}

Final Answer (given to the user):
{final_answer}

{session_section}{knowledge_section}"""

_REFLECT_SESSION_FIELD = """\
  "session_facts": "<bullet-point facts for follow-up questions in this conversation: \
pod names, namespace names, service names, error codes, statuses found. \
Be specific. Empty string only if truly nothing concrete was found.>" """

_REFLECT_KNOWLEDGE_FIELD = """\
  "persistent_insights": "<stable facts useful in future conversations: \
which namespaces exist, pod/service names and their roles, access limitations, \
tools or endpoints that don't work as expected, recurring patterns. \
Err on the side of including more rather than less. \
Empty string only if nothing new beyond what is already in the knowledge base above.>" """


def _build_reflect_prompt(
    task_ledger: str,
    progress_ledger: str,
    final_answer: str,
    session_section: str,
    knowledge_section: str,
    need_session: bool,
    need_knowledge: bool,
) -> str:
    """Build a minimal reflect prompt requesting only the fields that will be used."""
    header = _REFLECT_HEADER.format(
        task_ledger=task_ledger,
        progress_ledger=progress_ledger,
        final_answer=final_answer,
        session_section=session_section,
        knowledge_section=knowledge_section,
    )
    fields = []
    if need_session:
        fields.append(_REFLECT_SESSION_FIELD)
    if need_knowledge:
        fields.append(_REFLECT_KNOWLEDGE_FIELD)

    fields_str = ",\n".join(fields)
    return header + f"Return ONLY this JSON (no other text):\n{{\n{fields_str}\n}}"


# ── Knowledge file helpers ────────────────────────────────────────────────────

def _read_knowledge() -> str:
    if not os.path.exists(KNOWLEDGE_FILE):
        return ""
    with open(KNOWLEDGE_FILE, encoding="utf-8") as f:
        return f.read().strip()


def _append_knowledge(insights: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n\n---\n**{timestamp}**\n{insights.strip()}"
    with open(KNOWLEDGE_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


def _ensure_knowledge_file() -> None:
    if not os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
            f.write(
                "# Cluster Knowledge Base\n\n"
                "*Automatically updated by the Magentic agent after each conversation.*\n"
                "*Feel free to edit, correct, or delete entries manually.*\n"
            )


# ── Parse helper ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Extract JSON from an LLM response, tolerating markdown fences and surrounding text."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Local LLMs often add prose before/after the JSON object (e.g. "Here is the JSON: {...}")
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
        raise


# ── Reflect node ──────────────────────────────────────────────────────────────

class ReflectNode:
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, state: GraphState) -> dict:
        # Fast exit when both ledgers are disabled
        if not ENABLE_SESSION_LEDGER and not ENABLE_KNOWLEDGE_BASE:
            return {}

        task_ledger    = state.get("task_ledger", "(unavailable)")
        progress_ledger = state.get("progress_ledger", "(unavailable)")
        final_answer   = state.get("final_answer", "(unavailable)")

        # Optional sections injected into the prompt
        session_section  = ""
        knowledge_section = ""

        if ENABLE_SESSION_LEDGER:
            existing_session = (state.get("session_ledger") or "").strip()
            if existing_session:
                session_section = (
                    f"Current Session Ledger (facts already known this conversation):\n"
                    f"{existing_session}\n\n"
                )

        if ENABLE_KNOWLEDGE_BASE:
            _ensure_knowledge_file()
            kb_content = _read_knowledge()
            if kb_content:
                knowledge_section = (
                    f"Current Knowledge Base (persistent facts from past conversations):\n"
                    f"{kb_content}\n\n"
                )

        prompt = _build_reflect_prompt(
            task_ledger=task_ledger,
            progress_ledger=progress_ledger,
            final_answer=final_answer,
            session_section=session_section,
            knowledge_section=knowledge_section,
            need_session=ENABLE_SESSION_LEDGER,
            need_knowledge=ENABLE_KNOWLEDGE_BASE,
        )

        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]

        # Stream for TTFT tracking (same pattern as orchestrator / agents)
        t0 = time.monotonic()
        t_first: float | None = None
        response = None

        try:
            chunks = []
            async for chunk in self.llm.astream(messages):
                if t_first is None:
                    t_first = time.monotonic()
                chunks.append(chunk)
            if chunks:
                merged: AIMessageChunk = chunks[0]
                for c in chunks[1:]:
                    merged = merged + c
                response = AIMessage(
                    content=merged.content,
                    usage_metadata=getattr(merged, "usage_metadata", None),
                    response_metadata=getattr(merged, "response_metadata", {}),
                )
        except Exception:
            pass  # reflection is best-effort — never crash the main flow

        duration_ms = int((time.monotonic() - t0) * 1000)
        ttft_ms     = int((t_first - t0) * 1000) if t_first is not None else None

        if response is None:
            return {}

        usage = _extract_token_usage(response)
        call_stat = {
            "node":              "reflect",
            "prompt_tokens":     usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens":      usage["total_tokens"],
            "duration_ms":       duration_ms,
            "ttft_ms":           ttft_ms,
        }

        try:
            data = _parse_json(response.content)
        except (json.JSONDecodeError, ValueError):
            return {"last_call_stat": call_stat}

        update: dict = {"last_call_stat": call_stat}

        if ENABLE_SESSION_LEDGER:
            new_facts = (data.get("session_facts") or "").strip()
            if new_facts:
                existing = (state.get("session_ledger") or "").strip()
                separator = "\n" if existing else ""
                update["session_ledger"] = existing + separator + new_facts

        if ENABLE_KNOWLEDGE_BASE:
            new_insights = (data.get("persistent_insights") or "").strip()
            if new_insights:
                _append_knowledge(new_insights)

        return update
