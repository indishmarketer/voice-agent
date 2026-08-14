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


def transcript_text(turns: list[dict[str, Any]]) -> str:
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


def extract_phone(text: str) -> Optional[str]:
    match = _PHONE.search(text)
    return re.sub(r"[^\d+]", "", match.group(0)) if match else None


async def extract_contact_reply(text: str, api_key: Optional[str] = None,
                                model: Optional[str] = None) -> dict[str, Any]:
    """Live, mid-call extraction of name/email/phone from the caller's answer
    to the contact-collection question.

    Scoped to just that one reply, not the whole transcript, and fast enough
    to run while the caller is looking at the confirmation card waiting for
    it to fill in. A regex safety net covers cases the LLM call misses or the
    call itself fails - email and phone are the fields worth the redundancy,
    since a garbled voice-transcribed email is exactly what this whole flow
    exists to catch.
    """
    result: dict[str, Any] = {
        "name": None, "email": None, "phone": None, "declined": False,
    }
    try:
        raw = await llm.complete(
            [
                {"role": "system", "content": prompts.CONTACT_EXTRACTION_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=150,
            api_key=api_key,
            model=model,
        )
        parsed = llm.parse_json_object(raw)
        for key in ("name", "email", "phone"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip() and val.strip().lower() != "null":
                result[key] = val.strip()
        result["declined"] = bool(parsed.get("declined"))
    except Exception as exc:
        log.warning("live contact extraction failed: %s", exc)

    if not result["email"]:
        result["email"] = _spoken_email_fallback(text)
    if not result["phone"]:
        result["phone"] = extract_phone(text)
    return result


async def extract_confirmation_reply(text: str, api_key: Optional[str] = None,
                                     model: Optional[str] = None) -> dict[str, Any]:
    """Live extraction for the caller's reply to "is that correct?" after the
    backend reads their collected name/email/phone back to them. Separate
    from extract_contact_reply above because a plain "yes" here means
    something specific (confirmed=True), not just "no new field mentioned"."""
    result: dict[str, Any] = {
        "confirmed": False, "name": None, "email": None, "phone": None,
    }
    try:
        raw = await llm.complete(
            [
                {"role": "system", "content": prompts.CONFIRMATION_EXTRACTION_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=150,
            api_key=api_key,
            model=model,
        )
        parsed = llm.parse_json_object(raw)
        result["confirmed"] = bool(parsed.get("confirmed"))
        for key in ("name", "email", "phone"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip() and val.strip().lower() != "null":
                result[key] = val.strip()
    except Exception as exc:
        log.warning("confirmation extraction failed: %s", exc)
        # Fail safe on a plain yes, so a transient LLM error cannot strand a
        # caller who clearly agreed in an unwinnable loop.
        if text.strip().lower() in ("yes", "yeah", "yep", "correct", "that's right"):
            result["confirmed"] = True

    if not result["email"]:
        result["email"] = _spoken_email_fallback(text)
    if not result["phone"]:
        result["phone"] = extract_phone(text)
    return result


async def process_session(session_id: str, visitor_id: str,
                          turns: list[dict[str, Any]],
                          confirmed_contact: Optional[dict[str, Any]] = None,
                          account_id: Optional[int] = None,
                          api_key: Optional[str] = None,
                          model: Optional[str] = None) -> None:
    """Summarise the call, pull out contact details, persist and sync.

    `confirmed_contact` is the name/email/phone the caller explicitly
    confirmed on screen during the call, if they got that far. Those values
    are ground truth - a human corrected them - so they always win over
    whatever this post-call pass guesses from the full transcript. This pass
    still runs for its own sake even when contact was confirmed live: it is
    the only place company/problem/interest get filled in, and the call
    summary feeds the visitor's cross-session memory either way.
    """
    user_turns = [t for t in turns if t["role"] == "user"]
    if len(user_turns) < 1:
        return  # nothing was actually said

    transcript = transcript_text(turns)

    summary = ""
    try:
        summary = await llm.complete(
            [
                {"role": "system", "content": prompts.SUMMARY_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=140,
            api_key=api_key,
            model=model,
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
            api_key=api_key,
            model=model,
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
        data["phone"] = extract_phone(caller_said)

    if confirmed_contact:
        data.update({k: v for k, v in confirmed_contact.items()
                    if k in ("name", "email", "phone") and v})

    store.update_visitor_memory(
        visitor_id,
        summary=summary or None,
        name=data.get("name"),
        email=data.get("email"),
        phone=data.get("phone"),
    )

    lead_id = store.save_lead(session_id, visitor_id, data, transcript, account_id)
    log.info("lead %s saved (email=%s, account=%s)", lead_id,
             bool(data.get("email")), account_id)
    # Google Sheets sync (_push_to_sheet below) is retired in favour of CSV
    # export from /admin/leads - kept in the file only because
    # sheets-webhook.gs still references the same payload shape, not because
    # anything here still calls it.


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
