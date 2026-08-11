"""Fish Audio streaming text-to-speech over WebSocket.

The old code POSTed the whole reply and waited for a complete MP3. Here we open
the socket while the LLM is still thinking, push each clause the moment it is
ready, and forward raw PCM frames to the browser as they arrive. Audio starts
playing while the model is still writing.
"""
import asyncio
import contextlib
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

import ormsgpack
import websockets

from . import config

log = logging.getLogger("tts")

AudioSink = Callable[[bytes], Awaitable[None]]


class FishStream:
    """One synthesis session. Open early, feed clauses, then finish."""

    def __init__(self, voice_id: Optional[str] = None) -> None:
        self.voice_id = voice_id or config.FISH_MODEL_ID
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

    async def open(self, attempts: int = 2) -> None:
        headers = {
            "Authorization": f"Bearer {config.FISH_API_KEY}",
            "model": config.FISH_MODEL,
        }
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                self._ws = await websockets.connect(
                    config.FISH_WS_URL,
                    additional_headers=headers,
                    max_size=None,
                    open_timeout=8,
                    ping_interval=20,
                    ping_timeout=20,
                )
                break
            except Exception as exc:  # transient on a shared free tier
                last = exc
                log.warning("fish connect attempt %d failed: %s", attempt + 1, exc)
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25)
        if self._ws is None:
            raise last or RuntimeError("could not connect to Fish Audio")
        start = {
            "event": "start",
            "request": {
                "text": "",
                "reference_id": self.voice_id,
                "format": "pcm",
                "sample_rate": config.FISH_SAMPLE_RATE,
                "latency": config.FISH_LATENCY,
                "normalize": True,
                # Small chunks keep first-audio latency down.
                "chunk_length": 120,
                "min_chunk_length": 40,
            },
        }
        await self._ws.send(ormsgpack.packb(start))

    async def send_text(self, text: str) -> None:
        if not self._ws or not text.strip():
            return
        await self._ws.send(ormsgpack.packb({"event": "text", "text": text + " "}))

    async def flush(self) -> None:
        if self._ws:
            await self._ws.send(ormsgpack.packb({"event": "flush"}))

    async def finish(self) -> None:
        """Tell the server no more text is coming; it drains then closes."""
        if self._ws:
            try:
                await self._ws.send(ormsgpack.packb({"event": "stop"}))
            except Exception:
                pass

    async def audio(self) -> AsyncIterator[bytes]:
        """Yield PCM frames until the server reports it is finished."""
        if not self._ws:
            return
        async for message in self._ws:
            if not isinstance(message, (bytes, bytearray)):
                continue
            try:
                event = ormsgpack.unpackb(bytes(message))
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "audio":
                chunk = event.get("audio")
                if chunk:
                    yield bytes(chunk)
            elif kind == "finish":
                if event.get("reason") == "error":
                    log.warning("fish tts finished with error: %s", event)
                return
            elif kind == "log":
                log.debug("fish: %s", event.get("message"))

    async def close(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


async def synthesize(text: str, voice_id: Optional[str] = None) -> bytes:
    """One-shot synthesis. Used off the call path to pre-render filler clips."""
    stream = FishStream(voice_id)
    frames: list[bytes] = []
    try:
        await stream.open()
        await stream.send_text(text)
        await stream.flush()
        await stream.finish()
        async for frame in stream.audio():
            frames.append(frame)
    finally:
        await stream.close()
    return b"".join(frames)


async def speak(clauses: AsyncIterator[str], sink: AudioSink,
                cancel: asyncio.Event, voice_id: Optional[str] = None) -> str:
    """Drive a full turn: feed clauses in, push PCM out, honour barge-in.

    Returns the text that was actually spoken.
    """
    stream = FishStream(voice_id)
    spoken: list[str] = []

    # Opening the socket costs the best part of a second, and so does the first
    # LLM token. Start both at once rather than paying for them back to back.
    open_task = asyncio.create_task(stream.open())

    async def pump_text() -> None:
        opened = False
        try:
            async for clause in clauses:
                if cancel.is_set():
                    break
                spoken.append(clause)
                if not opened:
                    await open_task  # by now this has usually finished already
                    opened = True
                await stream.send_text(clause)
                await stream.flush()
        except Exception as exc:
            log.error("llm stream failed mid-turn: %s", exc)
        finally:
            if not opened:
                with contextlib.suppress(Exception):
                    await open_task
            await stream.finish()

    text_task = asyncio.create_task(pump_text())

    try:
        await open_task
    except Exception as exc:
        log.error("could not open Fish Audio stream: %s", exc)
        await asyncio.gather(text_task, return_exceptions=True)
        return " ".join(spoken).strip()

    produced = 0
    try:
        async for frame in stream.audio():
            if cancel.is_set():
                break
            produced += len(frame)
            await sink(frame)
    except websockets.ConnectionClosed:
        pass
    except Exception as exc:
        log.error("fish audio stream failed: %s", exc)
    finally:
        if not text_task.done():
            text_task.cancel()
        await asyncio.gather(text_task, return_exceptions=True)
        await stream.close()

    if spoken and not produced:
        # Text was generated but no audio came back. Worth shouting about:
        # the caller heard silence.
        log.error("fish returned no audio for %r", spoken[:80])

    return " ".join(spoken).strip()
