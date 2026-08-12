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

from . import config, tts

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

# Keyed by index into PHRASES, so a clip that fails to render never shifts the
# others out of alignment with their phrase.
_clips: dict[int, bytes] = {}
_warming = False


def _cache_dir() -> Path:
    # Key the cache on the phrases, voice and sample rate. Editing any of them
    # produces a new directory, so a stale clip can never be served under an
    # index whose phrase has changed.
    fingerprint = hashlib.sha1(
        ("|".join(PHRASES) + config.FISH_MODEL_ID +
         str(config.FISH_SAMPLE_RATE)).encode()
    ).hexdigest()[:10]
    path = config.DATA_DIR / "fillers" / fingerprint
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(index: int) -> Path:
    return _cache_dir() / f"{index:02d}.pcm"


def load_cached() -> int:
    """Load any clips already on disk. Returns how many were found."""
    _clips.clear()
    for index in range(len(PHRASES)):
        path = _cache_path(index)
        if path.exists() and path.stat().st_size > 1000:
            _clips[index] = path.read_bytes()
    return len(_clips)


async def warm() -> None:
    """Render any missing clips. Safe to call on startup; never blocks a call."""
    global _warming
    if _warming:
        return
    _warming = True
    try:
        if load_cached() == len(PHRASES):
            log.info("filler clips loaded from cache (%d)", len(_clips))
            return
        if not config.FISH_API_KEY:
            return

        for index, phrase in enumerate(PHRASES):
            if index in _clips:
                continue
            try:
                pcm = await tts.synthesize(phrase)
            except Exception as exc:
                log.warning("could not render filler %r: %s", phrase, exc)
                continue
            if len(pcm) > 1000:
                _cache_path(index).write_bytes(pcm)
                _clips[index] = pcm
            await asyncio.sleep(0.2)  # be gentle with the free tier

        log.info("filler clips ready (%d/%d)", len(_clips), len(PHRASES))
    finally:
        _warming = False


def _pick_from(names: list[str]) -> bytes:
    """A random clip from one tier, falling back to any clip we have."""
    indexes = [i for i, phrase in enumerate(PHRASES)
               if phrase in names and i in _clips]
    if indexes:
        return _clips[random.choice(indexes)]
    return random.choice(list(_clips.values())) if _clips else b""


def ack() -> bytes:
    """Short acknowledgement, played immediately."""
    return _pick_from(ACKS)


def bridge() -> bytes:
    """Longer clip, played only if the model is still thinking."""
    return _pick_from(BRIDGES)


def ready() -> bool:
    return bool(_clips)
