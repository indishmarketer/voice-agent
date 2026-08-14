"""Pollinations AI client.

Two modes:
  * stream_reply  - token-by-token SSE, used on the live call so the first
                    sentence can reach the TTS engine while the model is still
                    writing the second one.
  * complete      - one-shot, used off the critical path for summaries and lead
                    extraction after the call has ended.
"""
import json
import re
from typing import Any, AsyncIterator, Optional

import httpx

from . import config, integrations

_HEADERS = {"Content-Type": "application/json"}

# Break on sentence enders, but only once we have enough text to be worth
# synthesising. Commas count as a boundary after a longer run so the first audio
# starts sooner on long sentences.
_STRONG_END = re.compile(r"[.!?]['\")\]]?\s")
_SOFT_END = re.compile(r"[,;:]\s")
# Short enough that a greeting like "Hi there." is spoken immediately, long
# enough that a decimal point or "Mr." does not become its own chunk.
_MIN_STRONG = 8
_MIN_SOFT = 60


def _auth(api_key: Optional[str] = None) -> dict[str, str]:
    return {**_HEADERS, "Authorization": f"Bearer {api_key or integrations.pollinations_api_key()}"}


async def stream_reply(messages: list[dict[str, str]],
                       max_tokens: Optional[int] = None,
                       api_key: Optional[str] = None,
                       model: Optional[str] = None) -> AsyncIterator[str]:
    """Yield text deltas as the model produces them."""
    payload = {
        "model": model or integrations.pollinations_model(),
        "messages": messages,
        "stream": True,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
    }
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{config.POLLINATIONS_BASE}/chat/completions",
            headers=_auth(api_key),
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"Pollinations {response.status_code}: {body}")
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece


async def sentences(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Regroup a token stream into speakable clauses."""
    buffer = ""
    async for piece in deltas:
        buffer += piece
        while True:
            cut = _find_cut(buffer)
            if cut is None:
                break
            chunk, buffer = buffer[:cut].strip(), buffer[cut:]
            if chunk:
                yield chunk
    tail = buffer.strip()
    if tail:
        yield tail


def _find_cut(buffer: str) -> Optional[int]:
    # Scan every sentence end, not just the first: a too-short candidate must
    # not stop us finding the next valid one, or the clause never gets cut.
    for match in _STRONG_END.finditer(buffer):
        if match.end() >= _MIN_STRONG:
            return match.end()
    if len(buffer) >= _MIN_SOFT:
        soft = _SOFT_END.search(buffer, _MIN_SOFT // 2)
        if soft:
            return soft.end()
    return None


async def limit(clauses: AsyncIterator[str], max_chars: int) -> AsyncIterator[str]:
    """Stop a rambling model mid-reply.

    max_tokens is a suggestion that models routinely overshoot, and every extra
    sentence costs speech-synthesis quota and makes the caller wait. This is the
    hard stop.
    """
    used = 0
    async for clause in clauses:
        yield clause
        used += len(clause)
        if used >= max_chars:
            return


async def complete(messages: list[dict[str, str]], max_tokens: int = 300,
                   api_key: Optional[str] = None, model: Optional[str] = None) -> str:
    """Blocking single-shot call. Only used after a call ends."""
    payload = {
        "model": model or integrations.pollinations_model(),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            f"{config.POLLINATIONS_BASE}/chat/completions",
            headers=_auth(api_key),
            json=payload,
        )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """Models like to wrap JSON in prose or fences. Dig the object out."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
