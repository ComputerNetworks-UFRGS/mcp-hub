"""Shared utilities: token-usage extraction, JSON parsing, and streaming LLM helper."""
import re
import json
import time
from langchain_core.messages import AIMessage, AIMessageChunk


# ── Token usage ───────────────────────────────────────────────────────────────

def _extract_token_usage(response: AIMessage) -> dict:
    """
    Extract token counts from an AIMessage, trying multiple locations:
      1. usage_metadata  (LangChain standard: input_tokens / output_tokens)
      2. response_metadata.token_usage  (OpenAI legacy: prompt_tokens / completion_tokens)
    """
    meta = getattr(response, "usage_metadata", None) or {}
    if meta.get("input_tokens") or meta.get("output_tokens"):
        prompt     = int(meta.get("input_tokens",  0))
        completion = int(meta.get("output_tokens", 0))
        return {
            "prompt_tokens":     prompt,
            "completion_tokens": completion,
            "total_tokens":      int(meta.get("total_tokens", prompt + completion)),
        }

    rm = getattr(response, "response_metadata", None) or {}
    tu = rm.get("token_usage") or {}
    prompt     = int(tu.get("prompt_tokens",     0))
    completion = int(tu.get("completion_tokens", 0))
    return {
        "prompt_tokens":     prompt,
        "completion_tokens": completion,
        "total_tokens":      int(tu.get("total_tokens", prompt + completion)),
    }


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Extract JSON from an LLM response, tolerating markdown code fences and prose."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
        raise


# ── Streaming helper ──────────────────────────────────────────────────────────

async def astream_llm(llm, messages: list, config=None, **kwargs) -> tuple:
    """
    Stream an LLM call while measuring time-to-first-token (TTFT).

    TTFT = time from call start until the first chunk arrives.
    This separates provider/network latency from model generation time:
      - wait (TTFT):         provider queued / network round-trip
      - gen  (total − TTFT): model actually generating tokens

    Returns:
        (response: AIMessage | None, duration_ms: int, ttft_ms: int | None)
    """
    t0 = time.monotonic()
    t_first: float | None = None
    try:
        chunks: list[AIMessageChunk] = []
        async for chunk in llm.astream(messages, config=config, **kwargs):
            if t_first is None:
                t_first = time.monotonic()
            chunks.append(chunk)

        if chunks:
            merged: AIMessageChunk = chunks[0]
            for c in chunks[1:]:
                merged = merged + c

            response = AIMessage(
                content=merged.content,
                tool_calls=list(getattr(merged, "tool_calls", []) or []),
                usage_metadata=getattr(merged, "usage_metadata", None),
                response_metadata=getattr(merged, "response_metadata", {}),
                id=getattr(merged, "id", None),
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            ttft_ms     = int((t_first - t0) * 1000) if t_first is not None else None
            return response, duration_ms, ttft_ms

    except Exception:
        pass

    return None, int((time.monotonic() - t0) * 1000), None
