"""System prompt assembly: persona + knowledge base + what we remember.

The prompt has two layers, deliberately kept separate:

  FIXED_PREFIX     - identity, speaking format, and the CONTACT_MARKER
                      mechanism. Generated in code, never editable from admin.
                      Lead capture and TTS-safe output both depend on this
                      being present in every call, so it cannot be something
                      a text-box edit could accidentally delete.

  behaviour rules  - tone, priorities, what to ask about, when to wrap up.
                      Editable from /admin/settings, stored as plain text in
                      the settings table. This is raw text concatenated into
                      the prompt, never passed through .format() - so nothing
                      the admin types (including stray { or } characters) can
                      ever break prompt assembly, unlike the old single
                      PERSONA template this replaced.
"""
from typing import Any, Optional

from . import branding, config, knowledge, store

FIXED_PREFIX_TEMPLATE = """You are {agent_name}, the voice receptionist for {brand}.

You are speaking on a live phone call. Everything you write is read aloud by a
text-to-speech engine, so it must sound like natural speech.

HOW TO SPEAK
- One or two short sentences per reply. Never more than three.
- Plain spoken English. No markdown, no asterisks, no bullet points, no emoji.
- Write numbers, prices and URLs the way a person says them out loud.
- Never say you are an AI language model. You are the receptionist.
- If you are interrupted, drop what you were saying and answer the new question.

WHEN YOU DECIDE IT IS TIME TO COLLECT CONTACT DETAILS
- Do not ask for their name, email or phone number yourself - the system asks
  for those, one at a time, immediately after this reply. Just say something
  short and natural that transitions to it, like "Great, let me grab a few
  details from you."
- End that reply with the exact marker {contact_marker} on its own, with
  nothing after it. Say it in your head, never out loud - it is a signal to
  the system, not something to speak.
- Do not try to collect or repeat back contact details yourself in
  conversation after that point - the system owns that until it is done.
"""

# Emitted by the model as the very last thing in a turn, once it has decided
# to ask for contact details. Detected server-side and stripped before the
# reply is ever sent to text-to-speech - the caller must never hear it.
CONTACT_MARKER = "[[COLLECT_CONTACT]]"

SETTINGS_KEY_AGENT_RULES = "agent_rules"


def _default_behavior_rules() -> str:
    # A plain f-string, not .format() - this only ever runs on trusted code
    # (config values), never on admin-edited text, so there is no risk of a
    # stray brace in this default breaking anything.
    return f"""Answer from the company knowledge provided below. It is the
source of truth - if it does not cover something, say you will have the team
follow up rather than inventing details. Never invent prices, timelines or
claims.

Ask one question at a time, then stop and listen. Your goal is to understand
the caller's business problem, then capture a way to reach them.

Once you genuinely understand their problem - not before - decide it is time
to collect their name, email, and phone number so the team can follow up.
Say something short and natural like "Great, let me grab a few details from
you" - the system asks the specific questions right after, so do not list
name/email/phone yourself. Do not trigger this more than once in a call. If
they already declined, or their details are already on file, do not trigger
it again.

To end a call, thank them and mention {config.WEBSITE_URL}."""


def active_behavior_rules() -> str:
    """What /admin/settings shows and edits - the current effective rules,
    whether that is a saved override or the seed default."""
    return store.get_setting(SETTINGS_KEY_AGENT_RULES) or _default_behavior_rules()


def build_system_prompt(user_text: str, visitor: Optional[dict[str, Any]] = None,
                        history_hint: str = "",
                        account_id: Optional[int] = None) -> str:
    current_branding = branding.get_branding()
    fixed = FIXED_PREFIX_TEMPLATE.format(
        agent_name=current_branding["agent_name"],
        brand=current_branding["brand_name"],
        contact_marker=CONTACT_MARKER,
    )
    parts = [fixed, active_behavior_rules()]

    kb_context = knowledge.get_kb(account_id).context_for(user_text)
    if kb_context:
        parts.append("=== COMPANY KNOWLEDGE ===\n" + kb_context)

    memory = _memory_block(visitor, history_hint)
    if memory:
        parts.append("=== WHAT YOU REMEMBER ABOUT THIS CALLER ===\n" + memory)

    return "\n\n".join(parts)


def _memory_block(visitor: Optional[dict[str, Any]], history_hint: str) -> str:
    if not visitor:
        return ""
    lines: list[str] = []
    name = visitor.get("name")
    if name:
        lines.append(f"Name: {name}")
    if visitor.get("email"):
        lines.append(f"Email already on file: {visitor['email']}. Do not ask again.")
    if visitor.get("phone"):
        lines.append(f"Phone already on file: {visitor['phone']}.")
    if visitor.get("summary"):
        lines.append(f"Previous conversations: {visitor['summary']}")
    if history_hint:
        lines.append(f"Recent exchanges: {history_hint}")
    if lines and visitor.get("session_count", 0) > 0:
        lines.insert(0, "This caller has spoken to you before. Greet them as a "
                        "returning caller and do not re-ask what you already know.")
    return "\n".join(lines)


SUMMARY_PROMPT = """Summarise this sales call in at most 60 words for the agent's
own memory. Capture who they are, what business problem they have, what they were
told, and anything to follow up on. Write it as plain notes, no preamble."""

EXTRACTION_PROMPT = """Extract the caller's contact details and needs from this
transcript. Reply with ONLY a JSON object and nothing else, using exactly these
keys: name, email, phone, company, problem, interest.

Rules:
- Use null for anything not clearly stated. Never guess or invent.
- Emails are often spelled out in speech ("john at gmail dot com"). Convert them
  to normal form ("john@gmail.com").
- "problem" is one sentence describing what they need help with.
- "interest" is one of: automation, ai_agents, lead_generation, content,
  training, other, unknown."""

# Used live, mid-call, right after the agent has asked for contact details -
# scoped to just the caller's reply to that one question, not the whole
# transcript. Speed matters here (the caller is looking at a screen waiting
# for it), so this is deliberately narrower than EXTRACTION_PROMPT above.
CONTACT_EXTRACTION_PROMPT = """The caller was just asked for their name, email
and phone number on a voice call. Extract what they said. Reply with ONLY a
JSON object and nothing else, using exactly these keys: name, email, phone,
declined.

Rules:
- Use null for any field not stated in this reply. Never guess, never carry
  over a value from anywhere else.
- Emails are often spoken aloud ("john at gmail dot com") - convert to normal
  form ("john@gmail.com"). If what you get clearly is not a valid email shape
  even after conversion, use null rather than guessing.
- Phone numbers: strip filler words, keep only the digits and a leading + if
  given.
- "declined" is true only if the caller clearly refused or said not to contact
  them right now. Otherwise false."""

# Used right after the backend reads the collected name/email/phone back to
# the caller and asks "is that correct?" - a separate, narrower prompt from
# CONTACT_EXTRACTION_PROMPT above because the reply here means something
# different: a plain "yes" confirms, but the caller correcting one field
# ("no, my email is..." ) must NOT be read as a fresh decline.
CONFIRMATION_EXTRACTION_PROMPT = """The caller was just read back their name,
email and phone number and asked "is that correct?". Reply with ONLY a JSON
object and nothing else, using exactly these keys: confirmed, name, email,
phone.

Rules:
- "confirmed" is true only if the caller clearly agreed it is correct (e.g.
  "yes", "yeah", "that's right", "correct") with no correction in the same
  reply.
- If the caller corrected one or more fields instead of (or as well as)
  agreeing, set "confirmed" to false and put ONLY the corrected value(s) in
  name/email/phone - null for anything not corrected.
- If the caller said no, or seemed unsure, with no correction given, set
  "confirmed" to false and use null for name/email/phone.
- Emails are often spoken aloud ("john at gmail dot com") - convert to normal
  form ("john@gmail.com")."""
