"""AssemblyAI streaming access.

The browser talks to AssemblyAI directly - that is one network hop fewer than
proxying audio through us, and it keeps our container almost idle. It never sees
the API key: we mint a one-time temporary token instead.

The token is the enforcement point that matters. AssemblyAI honours
`max_session_duration_seconds` itself, so even a stolen token cannot consume
more than one short session of our free-tier hours.
"""
import logging
from urllib.parse import urlencode

import httpx

from . import config

log = logging.getLogger("stt")


async def mint_token() -> str:
    params = {
        "expires_in_seconds": 60,  # must be redeemed almost immediately
        "max_session_duration_seconds": max(60, config.SESSION_MAX_SECONDS),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{config.AAI_TOKEN_URL}?{urlencode(params)}",
            headers={"Authorization": config.ASSEMBLYAI_API_KEY},
        )
    if response.status_code != 200:
        log.error("assemblyai token mint failed %s: %s",
                  response.status_code, response.text[:200])
        raise RuntimeError("Speech service unavailable")
    token = response.json().get("token")
    if not token:
        raise RuntimeError("Speech service returned no token")
    return token


def websocket_url(token: str) -> str:
    """The URL the browser opens. Tuned for fast turn detection."""
    params = {
        "token": token,
        "sample_rate": config.AAI_SAMPLE_RATE,
        "encoding": "pcm_s16le",
        "speech_model": config.AAI_SPEECH_MODEL,
        "format_turns": "true",
        # Lower threshold + shorter silence = the agent starts replying sooner.
        "end_of_turn_confidence_threshold": "0.4",
        "min_turn_silence": "400",
        "max_turn_silence": "1100",
    }
    return f"{config.AAI_WS_BASE}?{urlencode(params)}"
