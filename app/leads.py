"""Post-call processing: summarise for memory, extract the lead, push to Sheets.

None of this runs during the call. It is fired off after hangup so it cannot add
a millisecond of latency to the conversation.
"""
import logging
import re
from typing import Any, Optional

import httpx

from . import config, llm, prompts, store

log = logging.getLogger("leads")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")


def _transcript_text(turns: list[dict[str, Any]]) -> str:
    label = {"user": "Caller", "assistant": "Agent"}
    return "\n".join(
        f"{label.get(t['role'], t['role'])}: {t['content']}" for t in turns
    )


def _spoken_email_fallback(text: str) -> Optional[str]:
    """Catch 'name at gmail dot com' when the model misses it."""
    direct = _EMAIL.search(text)
    if direct:
        return direct.group(0).lower()

    spoken = re.sub(r"\s+at\s+", "@", text, flags=re.IGNORECASE)
    spoken = re.sub(r"\s+dot\s+", ".", spoken, flags=re.IGNORECASE)
    spoken = re.sub(r"\s+(?:underscore|under score)\s+", "_", spoken,
                    flags=re.IGNORECASE)
    spoken = re.sub(r"\s+(?:dash|hyphen)\s+", "-", spoken, flags=re.IGNORECASE)
    # Close up spacing around the separators only. Stripping every space would
    # glue the preceding words onto the address ("itisbob@..." instead of "bob@...").
    spoken = re.sub(r"\s*([@.])\s*", r"\1", spoken)

    match = _EMAIL.search(spoken)
    return match.group(0).lower().strip(".") if match else None


async def process_session(session_id: str, visitor_id: str,
                          turns: list[dict[str, Any]]) -> None:
    """Summarise the call, pull out contact details, persist and sync."""
    user_turns = [t for t in turns if t["role"] == "user"]
    if len(user_turns) < 1:
        return  # nothing was actually said

    transcript = _transcript_text(turns)

    summary = ""
    try:
        summary = await llm.complete(
            [
                {"role": "system", "content": prompts.SUMMARY_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=140,
        )
    except Exception as exc:
        log.warning("summary failed for %s: %s", session_id, exc)

    data: dict[str, Any] = {}
    try:
        raw = await llm.complete(
            [
                {"role": "system", "content": prompts.EXTRACTION_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=300,
        )
        data = llm.parse_json_object(raw)
    except Exception as exc:
        log.warning("extraction failed for %s: %s", session_id, exc)

    data = {k: (v if isinstance(v, str) and v.strip() and v.lower() != "null" else None)
            for k, v in data.items()}

    caller_said = " ".join(t["content"] for t in user_turns)
    if not data.get("email"):
        data["email"] = _spoken_email_fallback(caller_said)
    if not data.get("phone"):
        match = _PHONE.search(caller_said)
        if match:
            data["phone"] = re.sub(r"[^\d+]", "", match.group(0))

    store.update_visitor_memory(
        visitor_id,
        summary=summary or None,
        name=data.get("name"),
        email=data.get("email"),
        phone=data.get("phone"),
    )

    lead_id = store.save_lead(session_id, visitor_id, data, transcript)
    log.info("lead %s saved (email=%s)", lead_id, bool(data.get("email")))

    if await _push_to_sheet(session_id, visitor_id, data, summary, transcript):
        store.mark_lead_synced(lead_id)


async def _push_to_sheet(session_id: str, visitor_id: str, data: dict[str, Any],
                         summary: str, transcript: str) -> bool:
    if not config.SHEETS_WEBHOOK_URL:
        return False
    payload = {
        "secret": config.SHEETS_WEBHOOK_SECRET,
        "session_id": session_id,
        "visitor_id": visitor_id,
        "name": data.get("name") or "",
        "email": data.get("email") or "",
        "phone": data.get("phone") or "",
        "company": data.get("company") or "",
        "problem": data.get("problem") or "",
        "interest": data.get("interest") or "",
        "summary": summary,
        "transcript": transcript,
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.post(config.SHEETS_WEBHOOK_URL, json=payload)
        if response.status_code < 300:
            return True
        log.warning("sheets webhook returned %s", response.status_code)
    except Exception as exc:
        log.warning("sheets webhook failed: %s", exc)
    return False
