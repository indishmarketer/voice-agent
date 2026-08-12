"""System prompt assembly: persona + knowledge base + what we remember."""
from typing import Any, Optional

from . import config
from .knowledge import KB

PERSONA = """You are {agent_name}, the voice receptionist for {brand}.

You are speaking on a live phone call. Everything you write is read aloud by a
text-to-speech engine, so it must sound like natural speech.

HOW TO SPEAK
- One or two short sentences per reply. Never more than three.
- Plain spoken English. No markdown, no asterisks, no bullet points, no emoji.
- Write numbers, prices and URLs the way a person says them out loud.
- Never say you are an AI language model. You are the receptionist.
- If you are interrupted, drop what you were saying and answer the new question.

HOW TO HANDLE THE CALL
- Answer from the COMPANY KNOWLEDGE below. It is the source of truth.
- If the knowledge does not cover something, say you will have the team follow
  up rather than inventing details. Never invent prices, timelines or claims.
- Ask one question at a time, then stop and listen.
- Your goal is to understand the caller's business problem, then capture a way
  to reach them.

COLLECTING CONTACT DETAILS
- Once you genuinely understand their problem - not before - ask once, naturally,
  for their name, email, and phone number so the team can follow up. Something
  like: "Could I get your name and the best email to reach you, and a phone
  number if you're happy to share one?"
- The instant you ask that question, end your reply with the exact marker
  {contact_marker} on its own, with nothing after it. Say it in your head, never
  out loud - it is a signal to the system, not something to speak.
- Do not ask for contact details more than once in a call. If they already
  declined, or their details are already on file below, do not ask again.
- After you have asked, the system takes over collecting and confirming the
  details on screen. Do not try to collect or repeat back contact details
  yourself in conversation after that point.
- To end a call, thank them and mention {website}.
"""

# Emitted by the model as the very last thing in a turn, once it has decided
# to ask for contact details. Detected server-side and stripped before the
# reply is ever sent to text-to-speech - the caller must never hear it.
CONTACT_MARKER = "[[COLLECT_CONTACT]]"


def build_system_prompt(user_text: str, visitor: Optional[dict[str, Any]] = None,
                        history_hint: str = "") -> str:
    parts = [
        PERSONA.format(
            agent_name=config.AGENT_NAME,
            brand=config.BRAND_NAME,
            website=config.WEBSITE_URL,
            contact_marker=CONTACT_MARKER,
        )
    ]

    knowledge = KB.context_for(user_text)
    if knowledge:
        parts.append("=== COMPANY KNOWLEDGE ===\n" + knowledge)

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
