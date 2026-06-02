"""
Post-investigation reflection node.

Feature flags (set to False to disable each ledger independently):
  ENABLE_SESSION_LEDGER  – accumulates facts within the current conversation
  ENABLE_KNOWLEDGE_BASE  – appends persistent insights to knowledge.md
"""

import os
import json
import re
from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from state import GraphState

# ── Feature flags ─────────────────────────────────────────────────────────────
ENABLE_SESSION_LEDGER = True
ENABLE_KNOWLEDGE_BASE = True

KNOWLEDGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.md")

# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a reflection assistant for a Kubernetes diagnostic system.
Your job is to extract useful knowledge from a completed investigation.
Always respond ONLY with valid JSON. No text outside the JSON block."""

REFLECT_PROMPT = """\
An investigation has just finished. Extract knowledge from it.

Task Ledger (what was planned):
{task_ledger}

Progress Ledger (what was found):
{progress_ledger}

Final Answer (given to the user):
{final_answer}

{session_section}{knowledge_section}\
You must return a JSON object with exactly these two keys:

{{
  "session_facts": "<bullet-point facts useful for follow-up questions in this \
conversation: pod names, namespace names, service names, error codes, statuses found. \
Be specific. Empty string only if truly nothing concrete was found.>",
  "persistent_insights": "<stable facts about this cluster useful in future \
conversations: which namespaces exist, pod/service names and their roles, \
access limitations, tools or endpoints that don't work as expected, \
recurring patterns. Err on the side of including more rather than less. \
Empty string only if nothing new beyond what is already in the knowledge base above.>"
}}

Rules:
- session_facts: include names, IDs, states discovered. If pods or services were \
  listed, include them here.
- persistent_insights: include namespace layouts, service names, and any cluster \
  quirks. Omit only if the exact same fact is already in the knowledge base above."""


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

        prompt = REFLECT_PROMPT.format(
            task_ledger=task_ledger,
            progress_ledger=progress_ledger,
            final_answer=final_answer,
            session_section=session_section,
            knowledge_section=knowledge_section,
        )

        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]
        response = await self.llm.ainvoke(messages)

        try:
            data = _parse_json(response.content)
        except (json.JSONDecodeError, ValueError):
            # Reflection is best-effort — never crash the main flow
            return {}

        update: dict = {}

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
