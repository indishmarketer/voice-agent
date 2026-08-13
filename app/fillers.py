"""Pre-rendered acknowledgement clips that mask model latency.

Even with everything streaming, there is an unavoidable ~1.3s wait for the
first token from the language model. A real receptionist fills that gap with
"sure" or "right, let me see" - so we do the same. The clip starts playing the
instant the caller stops talking, and the real answer is queued straight after
it, which is what makes the agent feel immediate rather than laggy.

Clips are synthesised once and cached on disk, so a redeploy costs a handful of
short sentences and every call after that is free.
"""
import asyncio
import hashlib
import logging
import random
from pathlib import Path

from . import config, integrations, tts

log = logging.getLogger("fillers")

# Two tiers. An ACK goes out the instant the caller stops talking. If the model
# is still thinking a second later, a BRIDGE follows it, which is what a real
# receptionist does rather than going silent.
#
# Every phrase here must make sense after ANY kind of caller utterance - a
# question, a statement, a trailing-off half-thought, or a turn the endpointer
# cut early on a thinking pause. That rules out anything that presumes what was
# just said, e.g. "good question" said back to a statement, or "let me check
# that for you" said back to a caller who was just describing their business.
# Neutral acknowledgement only - never a reaction to content we have not
# actually processed yet.
ACKS = [
    "Mm-hmm.",
    "Okay.",
    "Right.",
    "I see.",
    "Got it.",
]

BRIDGES = [
    "One moment.",
    "Just a second.",
    "Give me a moment to think about that.",
    "Okay, one second.",
]

PHRASES = ACKS + BRIDGES

# Keyed by (voice_id, index into PHRASES) - sub-accounts can pick a different
# Fish Audio voice per account (see voices.py), so the acknowledgement clip
# has to match whichever voice is about to speak the real reply, or the
# switch mid-turn sounds broken. A clip that fails to render never shifts
# the others out of alignment with their phrase.
_clips: dict[tuple[str, int], bytes] = {}
_warming = False


def _cache_dir(voice_id: str) -> Path:
    # Key the cache on the phrases, voice and sample rate. Editing any of them
    # produces a new directory, so a stale clip can never be served under an
    # index whose phrase has changed.
    fingerprint = hashlib.sha1(
        ("|".join(PHRASES) + voice_id + str(config.FISH_SAMPLE_RATE)).encode()
    ).hexdigest()[:10]
    path = config.DATA_DIR / "fillers" / fingerprint
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(voice_id: str, index: int) -> Path:
    return _cache_dir(voice_id) / f"{index:02d}.pcm"


def load_cached(voice_ids: list[str]) -> int:
    """Load any clips already on disk, for every voice given. Returns how
    many were found in total."""
    _clips.clear()
    for voice_id in voice_ids:
        for index in range(len(PHRASES)):
            path = _cache_path(voice_id, index)
            if path.exists() and path.stat().st_size > 1000:
                _clips[(voice_id, index)] = path.read_bytes()
    return len(_clips)


async def warm(voice_ids: list[str]) -> None:
    """Render any missing clips, for every voice given. Safe to call on
    startup; never blocks a call."""
    global _warming
    if _warming:
        return
    _warming = True
    try:
        wanted = len(voice_ids) * len(PHRASES)
        if load_cached(voice_ids) == wanted:
            log.info("filler clips loaded from cache (%d)", len(_clips))
            return
        if not integrations.fish_api_key():
            return

        for voice_id in voice_ids:
            for index, phrase in enumerate(PHRASES):
                if (voice_id, index) in _clips:
                    continue
                try:
                    pcm = await tts.synthesize(phrase, voice_id=voice_id)
                except Exception as exc:
                    log.warning("could not render filler %r (voice %s): %s",
                               phrase, voice_id, exc)
                    continue
                if len(pcm) > 1000:
                    _cache_path(voice_id, index).write_bytes(pcm)
                    _clips[(voice_id, index)] = pcm
                await asyncio.sleep(0.2)  # be gentle with the free tier

        log.info("filler clips ready (%d/%d)", len(_clips), wanted)
    finally:
        _warming = False


def _pick_from(names: list[str], voice_id: str, fallback_voice_id: str) -> bytes:
    """A random clip from one tier in this voice, falling back to the
    default voice's clips (better than dead air) if this one has none yet."""
    for vid in (voice_id, fallback_voice_id):
        indexes = [i for i, phrase in enumerate(PHRASES)
                  if phrase in names and (vid, i) in _clips]
        if indexes:
            return _clips[(vid, random.choice(indexes))]
    any_clips = list(_clips.values())
    return random.choice(any_clips) if any_clips else b""


def ack(voice_id: str, fallback_voice_id: str) -> bytes:
    """Short acknowledgement, played immediately."""
    return _pick_from(ACKS, voice_id, fallback_voice_id)


def bridge(voice_id: str, fallback_voice_id: str) -> bytes:
    """Longer clip, played only if the model is still thinking."""
    return _pick_from(BRIDGES, voice_id, fallback_voice_id)


def ready(voice_id: str, fallback_voice_id: str) -> bool:
    return any(vid == voice_id or vid == fallback_voice_id for vid, _ in _clips)
