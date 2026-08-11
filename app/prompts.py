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
- Once you understand their problem, ask for their email address so the team can
  send a plan. Ask naturally, once. If they decline, do not ask again.
- When the caller repeats an email or phone number back, read it back to confirm.
- To end a call, thank them and mention {website}.
"""


def build_system_prompt(user_text: str, visitor: Optional[dict[str, Any]] = None,
                        history_hint: str = "") -> str:
    parts = [
        PERSONA.format(
            agent_name=config.AGENT_NAME,
            brand=config.BRAND_NAME,
            website=config.WEBSITE_URL,
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
