"""FastAPI application: session issuing, the live call websocket, and admin.

Timeline of one turn, which is where the old version lost all its time:

    t+0ms    AssemblyAI reports end_of_turn, browser sends the text
    t+5ms    we open the Fish Audio socket AND start the LLM stream together
    t+400ms  first clause is complete -> pushed straight into Fish
    t+700ms  first PCM frames arrive -> forwarded to the browser -> audio starts

Nothing waits for the full reply at any stage.
"""
import asyncio
import contextlib
import datetime
import logging
import time
import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import (
    Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile, File,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, fillers, leads, llm, prompts, security, stt, store, tts
from .knowledge import KB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agent")

app = FastAPI(title="Indish Marketer Voice Agent", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


def _asset(path: str) -> str:
    """URL for a static file with a cache-busting version query string.

    Without this, browsers were free to cache style.css/app.js indefinitely -
    the server sent Last-Modified/ETag but no explicit Cache-Control, which
    leaves the caching duration up to each browser's own heuristics. A CSS
    fix could deploy cleanly and still never reach a returning visitor,
    because their browser never asked for a new copy. The version here is
    the file's on-disk mtime, which changes on every deploy (the Docker
    build re-copies the source tree), so the URL itself changes whenever the
    file's content actually does - forcing a fresh fetch without needing any
    build step or manual cache-clearing on either end.
    """
    full = config.BASE_DIR / "static" / path
    try:
        version = int(full.stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


templates.env.globals["asset"] = _asset

VISITOR_COOKIE = "im_visitor"
COOKIE_MAX_AGE = 365 * 24 * 3600


@app.on_event("startup")
async def _startup() -> None:
    store.init()
    KB.load()
    missing = config.missing_required()
    if missing:
        log.error("MISSING REQUIRED ENV VARS: %s - the agent will refuse calls",
                  ", ".join(missing))
    if not config.ALLOWED_ORIGINS:
        log.warning("ALLOWED_ORIGINS is empty - set it to your domain before "
                    "going public, otherwise any site can embed this agent.")
    log.info("knowledge base: %s", KB.stats())
    fillers.load_cached()
    if config.ENABLE_FILLERS and not missing:
        # Rendered in the background so the container reports healthy at once.
        asyncio.create_task(fillers.warm())


# --- Visitor identity -------------------------------------------------------

def _visitor_id_from(request: Request) -> tuple[str, bool]:
    """Returns (visitor_id, is_new)."""
    raw = request.cookies.get(VISITOR_COOKIE, "")
    payload = security.verify(raw) if raw else None
    if payload and payload.get("vid"):
        return payload["vid"], False
    return uuid.uuid4().hex, True


def _set_visitor_cookie(response: Response, visitor_id: str) -> None:
    response.set_cookie(
        VISITOR_COOKIE,
        security.sign({"vid": visitor_id}, COOKIE_MAX_AGE),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )


# --- Pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    visitor_id, is_new = _visitor_id_from(request)
    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "brand": config.BRAND_NAME,
            "agent_name": config.AGENT_NAME,
            "turnstile_site_key": config.TURNSTILE_SITE_KEY,
        },
    )
    if is_new:
        _set_visitor_cookie(response, visitor_id)
    return response


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


# --- Session issuing --------------------------------------------------------

@app.post("/api/session/start")
async def start_session(request: Request) -> Response:
    if config.missing_required():
        return JSONResponse(
            {"error": "The agent is not configured yet."}, status_code=503
        )

    origin = request.headers.get("origin", "")
    if origin and not security.origin_allowed(origin):
        return JSONResponse({"error": "Not allowed from this site."}, status_code=403)

    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    ip = security.client_ip(request)
    ip_hash = security.hash_ip(ip)
    visitor_id, is_new = _visitor_id_from(request)

    # The owner testing their own deployed site would otherwise trip the same
    # per-IP daily cap as the public - incognito clears the visitor cookie but
    # not the IP. A token only the owner has (never shipped in the client
    # bundle - see static/app.js) skips both the bot check and the quotas.
    is_owner = security.is_admin(request.headers.get("x-owner-token", ""))

    if not is_owner and not await security.verify_turnstile(
        body.get("turnstile_token", ""), ip
    ):
        return JSONResponse(
            {"error": "We could not verify you are human. Please reload and retry."},
            status_code=403,
        )

    if not is_owner:
        try:
            security.enforce_quotas(ip_hash, visitor_id)
        except security.Denied as denied:
            log.info("session denied (%s) ip=%s", denied.reason, ip_hash[:8])
            return JSONResponse({"error": denied.message}, status_code=denied.status)

    session_id = uuid.uuid4().hex
    try:
        security.acquire_slot(session_id)
    except security.Denied as denied:
        return JSONResponse({"error": denied.message}, status_code=denied.status)

    try:
        aai_token = await stt.mint_token()
    except Exception as exc:
        security.release_slot(session_id)
        log.error("token mint failed: %s", exc)
        return JSONResponse(
            {"error": "Speech service is unavailable right now."}, status_code=503
        )

    store.touch_visitor(visitor_id)
    store.create_session(session_id, visitor_id, ip_hash)

    payload = {
        "session_id": session_id,
        "session_token": security.sign(
            {"sid": session_id, "vid": visitor_id},
            config.SESSION_MAX_SECONDS + 120,
        ),
        "stt_ws_url": stt.websocket_url(aai_token),
        "stt_sample_rate": config.AAI_SAMPLE_RATE,
        "audio_sample_rate": config.FISH_SAMPLE_RATE,
        "max_seconds": config.SESSION_MAX_SECONDS,
    }
    response = JSONResponse(payload)
    if is_new:
        _set_visitor_cookie(response, visitor_id)
    return response


# --- The live call ----------------------------------------------------------

class Call:
    """State for one websocket conversation."""

    def __init__(self, ws: WebSocket, session_id: str, visitor_id: str) -> None:
        self.ws = ws
        self.session_id = session_id
        self.visitor_id = visitor_id
        self.started = time.monotonic()
        self.history: list[dict[str, str]] = []
        self.turn_count = 0
        self.stt_seconds = 0.0
        self.cancel = asyncio.Event()
        self.speaking: Optional[asyncio.Task] = None
        # The whole in-flight turn. Held separately from `speaking` so the
        # receive loop can cancel a turn the moment the caller interrupts.
        self.turn: Optional[asyncio.Task] = None
        # Set as soon as the first frame of *real* (non-filler) audio goes out.
        self.audio_started = asyncio.Event()
        self.visitor = store.touch_visitor(visitor_id)

        # Lead capture: the LLM asks for contact details in its own words and
        # signals it with a marker (see prompts.CONTACT_MARKER); the backend
        # then takes over collecting and confirming them on screen rather than
        # trusting voice transcription of an email address.
        self.awaiting_contact = False
        self.contact_fields: dict[str, Optional[str]] = {
            "name": None, "email": None, "phone": None,
        }
        self.contact_nudged = False
        # Once True, the marker is ignored for the rest of the call - covers
        # confirm, skip, and decline, so the model asking again cannot reopen
        # the form after the caller has already answered.
        self.contact_flow_done = False
        # What the caller explicitly confirmed on screen, if they got that
        # far. Passed to the post-call pass, which must never overwrite it.
        self.contact_confirmed_data: Optional[dict[str, Any]] = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, config.SESSION_MAX_SECONDS - self.elapsed)

    async def send_json(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.ws.send_json(payload)

    async def send_audio(self, frame: bytes, real: bool = True) -> None:
        if real:
            self.audio_started.set()
        with contextlib.suppress(Exception):
            await self.ws.send_bytes(frame)

    async def stop_speaking(self) -> None:
        """Abandon whatever the agent is currently saying, immediately."""
        self.cancel.set()
        # Kill the synthesis first so no further frames are queued, then unwind
        # the turn that owns it.
        for task in (self.speaking, self.turn):
            if task and not task.done():
                task.cancel()
        pending = [t for t in (self.speaking, self.turn) if t and not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.speaking = None
        self.turn = None


async def _once(text: str) -> AsyncIterator[str]:
    yield text


def _greeting_for(visitor: dict[str, Any]) -> str:
    name = visitor.get("name")
    if visitor.get("session_count", 0) > 1 and name:
        return (f"Hi {name}, welcome back to {config.BRAND_NAME}. "
                "What can I help you with today?")
    if visitor.get("session_count", 0) > 1:
        return (f"Welcome back to {config.BRAND_NAME}. "
                "What can I help you with today?")
    return config.GREETING


def _history_hint(visitor_id: str) -> str:
    turns = store.recent_turns(visitor_id, limit=6)
    if not turns:
        return ""
    return " | ".join(f"{t['role']}: {t['content'][:120]}" for t in turns)


@app.websocket("/ws/agent")
async def agent_socket(ws: WebSocket) -> None:
    token = ws.query_params.get("t", "")
    claims = security.verify(token)
    if not claims:
        await ws.close(code=4401)
        return

    origin = ws.headers.get("origin", "")
    if origin and not security.origin_allowed(origin):
        await ws.close(code=4403)
        return

    session_id = claims["sid"]
    visitor_id = claims["vid"]

    await ws.accept()
    call = Call(ws, session_id, visitor_id)
    log.info("call %s started (visitor %s)", session_id[:8], visitor_id[:8])

    watchdog = asyncio.create_task(_watchdog(call))
    try:
        await _run_call(call)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("call %s failed: %s", session_id[:8], exc)
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await call.stop_speaking()
        security.release_slot(session_id)
        store.end_session(session_id, call.stt_seconds, call.turn_count)
        with contextlib.suppress(Exception):
            await ws.close()
        # Summarising and lead extraction happen off the call path.
        if call.history:
            asyncio.create_task(
                _finalise(session_id, visitor_id, list(call.history),
                         call.contact_confirmed_data)
            )
        log.info("call %s ended after %.0fs, %d turns",
                 session_id[:8], call.elapsed, call.turn_count)


async def _finalise(session_id: str, visitor_id: str,
                    history: list[dict[str, str]],
                    confirmed_contact: Optional[dict[str, Any]] = None) -> None:
    with contextlib.suppress(Exception):
        await leads.process_session(session_id, visitor_id, history, confirmed_contact)


async def _watchdog(call: Call) -> None:
    """Hard hangup at the session limit, regardless of what the client does."""
    try:
        await asyncio.sleep(config.SESSION_MAX_SECONDS)
        await call.send_json({
            "type": "ended",
            "reason": "time_limit",
            "message": "That is all the time this demo allows. "
                       f"Visit {config.WEBSITE_URL} to keep the conversation going.",
        })
        await asyncio.sleep(0.4)
        with contextlib.suppress(Exception):
            await call.ws.close(code=4000)
    except asyncio.CancelledError:
        raise


async def _run_call(call: Call) -> None:
    while True:
        try:
            message = await asyncio.wait_for(
                call.ws.receive_json(), timeout=config.SESSION_IDLE_SECONDS
            )
        except asyncio.TimeoutError:
            await call.send_json({
                "type": "ended", "reason": "idle",
                "message": "I did not hear anything, so I will hang up. "
                           f"Visit {config.WEBSITE_URL} any time.",
            })
            return
        except (WebSocketDisconnect, RuntimeError):
            return

        kind = message.get("type")

        if kind == "start":
            await _start_turn(call, _handle_greeting(call))

        elif kind == "user_turn":
            text = (message.get("text") or "").strip()
            if text:
                await _start_turn(
                    call, _handle_turn(call, text[: config.MAX_USER_CHARS_PER_TURN])
                )

        elif kind == "barge_in":
            await call.stop_speaking()
            await call.send_json({"type": "status", "state": "listening"})

        elif kind == "contact_confirmed":
            await _start_turn(call, _handle_contact_confirmed(call, message))

        elif kind == "contact_skip":
            await _start_turn(call, _handle_contact_skip(call))

        elif kind == "stt_usage":
            # Client-reported, used for accounting and the daily budget.
            with contextlib.suppress(Exception):
                call.stt_seconds = max(
                    call.stt_seconds, float(message.get("seconds") or 0)
                )

        elif kind == "end":
            return


async def _start_turn(call: Call, coro) -> None:
    """Run a turn in the background.

    The receive loop must stay free to read the next message. If it blocked on
    the turn, a barge-in could not be noticed until the agent had already
    finished talking - which is precisely when it is useless.
    """
    await call.stop_speaking()

    async def guarded() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("turn failed: %s", exc)

    call.turn = asyncio.create_task(guarded())


async def _bridge_if_slow(call: Call) -> None:
    """If the model is still thinking once the acknowledgement has played,
    say one more natural thing rather than leaving dead air."""
    try:
        await asyncio.wait_for(
            call.audio_started.wait(), timeout=config.FILLER_BRIDGE_AFTER
        )
    except asyncio.TimeoutError:
        if call.cancel.is_set():
            return
        clip = fillers.bridge()
        if clip:
            await call.send_audio(clip, real=False)
    except asyncio.CancelledError:
        raise


async def _handle_greeting(call: Call) -> None:
    text = _greeting_for(call.visitor)
    call.history.append({"role": "assistant", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "assistant", text)
    await _speak(call, _once(text), announce=text)


async def _handle_turn(call: Call, text: str) -> None:
    if call.turn_count >= config.MAX_TURNS_PER_SESSION:
        await call.send_json({
            "type": "ended", "reason": "turn_limit",
            "message": f"We have covered a lot. Visit {config.WEBSITE_URL} "
                       "and the team will take it from here.",
        })
        return

    # No stop_speaking() here: this coroutine *is* call.turn, so cancelling
    # would cancel itself. _start_turn already cleared the previous turn.
    call.cancel = asyncio.Event()

    call.history.append({"role": "user", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "user", text)
    await call.send_json({"type": "user_text", "text": text})
    await call.send_json({"type": "status", "state": "thinking"})

    # While the contact card is showing, the caller's replies go to a fast,
    # scoped extraction pass instead of the main LLM - see _handle_contact_reply.
    # This does not count against the turn limit; it is bookkeeping, not
    # conversation.
    if call.awaiting_contact:
        await _handle_contact_reply(call, text)
        return

    call.turn_count += 1
    await _respond_with_llm(call, text)


async def _respond_with_llm(call: Call, text: str) -> None:
    """Generate and speak a normal conversational reply.

    Also used to let the model react naturally after the caller declines or
    skips the contact card, so the conversation does not just go silent.
    """
    # Acknowledge instantly. The caller hears a reply within a few hundred
    # milliseconds while the model is still thinking, and the real answer is
    # queued straight behind it by the browser's audio scheduler.
    bridge_task: Optional[asyncio.Task] = None
    if config.ENABLE_FILLERS and fillers.ready():
        call.audio_started.clear()
        clip = fillers.ack()
        if clip:
            await call.send_json({"type": "agent_start"})
            await call.send_audio(clip, real=False)
            bridge_task = asyncio.create_task(_bridge_if_slow(call))

    system = prompts.build_system_prompt(
        text, call.visitor, _history_hint(call.visitor_id)
    )
    if call.contact_flow_done:
        system += ("\n\nContact details have already been asked for this call - "
                   "do not ask again, even if you would otherwise.")
    if call.remaining < 25:
        system += ("\n\nThe call is about to end. Wrap up warmly in one sentence "
                   f"and point them to {config.WEBSITE_URL}.")

    messages = [{"role": "system", "content": system}]
    messages += call.history[-config.HISTORY_TURNS:]

    marker_flag = {"seen": False}
    clauses = _strip_contact_marker(
        llm.limit(llm.sentences(llm.stream_reply(messages)), config.MAX_REPLY_CHARS),
        marker_flag,
    )
    try:
        spoken = await _speak(call, clauses)
    finally:
        if bridge_task and not bridge_task.done():
            bridge_task.cancel()
            await asyncio.gather(bridge_task, return_exceptions=True)

    if spoken:
        call.history.append({"role": "assistant", "content": spoken})
        store.add_turn(call.session_id, call.visitor_id, "assistant", spoken)

    if marker_flag["seen"] and not call.contact_flow_done:
        call.awaiting_contact = True
        call.contact_nudged = False
        await call.send_json({"type": "show_contact_form", **call.contact_fields})


async def _strip_contact_marker(clauses: AsyncIterator[str],
                                flag: dict[str, bool]) -> AsyncIterator[str]:
    """Remove prompts.CONTACT_MARKER from the clause stream before it can ever
    reach text-to-speech, and record whether it was seen.

    The model is told to send it as its own clause at the very end of a turn,
    which is the common case and arrives cleanly via llm.sentences()'s final
    tail-yield. The suffix check is defensive, in case the model glues it onto
    the last sentence instead.
    """
    marker = prompts.CONTACT_MARKER
    async for clause in clauses:
        stripped = clause.strip()
        if stripped == marker:
            flag["seen"] = True
            continue
        if stripped.endswith(marker):
            flag["seen"] = True
            remainder = stripped[: -len(marker)].strip()
            if remainder:
                yield remainder
            continue
        yield clause


async def _handle_contact_reply(call: Call, text: str) -> None:
    """The caller's spoken answer to 'could I get your name, email, phone'."""
    data = await leads.extract_contact_reply(text)

    if data.get("declined"):
        call.awaiting_contact = False
        call.contact_flow_done = True
        await call.send_json({"type": "hide_contact_form"})
        await _respond_with_llm(call, text)
        return

    changed = False
    for key in ("name", "email", "phone"):
        if data.get(key):
            call.contact_fields[key] = data[key]
            changed = True

    if changed:
        await call.send_json({"type": "contact_fields", **call.contact_fields})

    if not call.contact_nudged:
        call.contact_nudged = True
        nudge = (
            "Great, please check the details on screen and confirm."
            if changed else
            "Sorry, I did not quite catch that - you can also just type your "
            "name, email and phone number on screen."
        )
        call.history.append({"role": "assistant", "content": nudge})
        store.add_turn(call.session_id, call.visitor_id, "assistant", nudge)
        await _speak(call, _once(nudge), announce=nudge)
    else:
        await call.send_json({"type": "status", "state": "listening"})


async def _handle_contact_confirmed(call: Call, message: dict[str, Any]) -> None:
    """The caller edited/reviewed the card and clicked Confirm."""
    name = (message.get("name") or "").strip()[:120] or None
    email = (message.get("email") or "").strip().lower()[:200] or None
    phone = (message.get("phone") or "").strip()[:40] or None

    call.contact_fields = {"name": name, "email": email, "phone": phone}
    call.contact_confirmed_data = dict(call.contact_fields)
    call.awaiting_contact = False
    call.contact_flow_done = True

    transcript = leads.transcript_text(call.history)
    lead_id = store.save_lead(
        call.session_id, call.visitor_id, call.contact_fields, transcript
    )
    store.update_visitor_memory(call.visitor_id, name=name, email=email, phone=phone)
    log.info("lead %s confirmed live for session %s", lead_id, call.session_id[:8])

    await call.send_json({"type": "contact_saved"})

    thanks = ("Thank you, that is saved. Our team will reach out soon. Is there "
             "anything else I can help with?")
    call.history.append({"role": "assistant", "content": thanks})
    store.add_turn(call.session_id, call.visitor_id, "assistant", thanks)
    await _speak(call, _once(thanks), announce=thanks)


async def _handle_contact_skip(call: Call) -> None:
    """The caller clicked 'not now' instead of confirming."""
    call.awaiting_contact = False
    call.contact_flow_done = True
    await call.send_json({"type": "hide_contact_form"})

    text = "No thanks, not right now."
    call.history.append({"role": "user", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "user", text)
    await _respond_with_llm(call, text)


async def _speak(call: Call, clauses: AsyncIterator[str],
                 announce: str = "") -> str:
    """Run one synthesis turn as a cancellable task so barge-in can kill it."""
    call.cancel = asyncio.Event()
    await call.send_json({"type": "agent_start"})
    if announce:
        await call.send_json({"type": "agent_text", "text": announce})

    async def relay(clause_stream: AsyncIterator[str]) -> AsyncIterator[str]:
        async for clause in clause_stream:
            if not announce:
                await call.send_json({"type": "agent_text", "text": clause})
            yield clause

    async def run() -> str:
        return await tts.speak(relay(clauses), call.send_audio, call.cancel)

    task = asyncio.create_task(run())
    call.speaking = task
    try:
        spoken = await task
    except asyncio.CancelledError:
        await call.send_json({"type": "agent_cancelled"})
        return ""
    except Exception as exc:
        log.error("speak failed: %s", exc)
        await call.send_json({
            "type": "error",
            "message": "Sorry, I had trouble speaking just then. Please try again.",
        })
        # Without this the client stays on whatever status "agent_start" set
        # (usually "Speaking...") with nothing left to move it forward - the
        # error banner shows, but the call looks frozen underneath it.
        await call.send_json({"type": "status", "state": "listening"})
        return ""
    finally:
        call.speaking = None

    await call.send_json({"type": "agent_done"})
    await call.send_json({"type": "status", "state": "listening"})
    return spoken


# --- Admin ------------------------------------------------------------------

# Fixed filenames for the core knowledge base, so the admin page can offer a
# dedicated upload slot per topic that always replaces the right file
# regardless of what the uploaded file happens to be named locally.
KNOWLEDGE_TARGETS = {
    "company": "00-company.md",
    "services": "01-services.md",
    "faq": "02-faq.md",
    "pricing": "03-pricing.md",
}


def _require_admin(request: Request) -> None:
    token = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    if not security.is_admin(token):
        raise HTTPException(status_code=404, detail="Not found")


def _knowledge_file_status() -> dict[str, dict[str, Any]]:
    status = {}
    for key, filename in KNOWLEDGE_TARGETS.items():
        path = config.KNOWLEDGE_DIR / filename
        if path.exists():
            stat = path.stat()
            status[key] = {
                "filename": filename,
                "exists": True,
                "characters": stat.st_size,
                "updated": datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.timezone.utc
                ).strftime("%b %d, %H:%M"),
            }
        else:
            status[key] = {"filename": filename, "exists": False,
                          "characters": 0, "updated": None}
    return status


def _admin_base_context(request: Request, active_page: str) -> dict[str, Any]:
    """Shared context every admin page needs - the sidebar, the theme toggle,
    and whatever identifies the logged-in owner (None until magic-link login
    is wired in, which is fine: the sidebar falls back to showing "token
    access" instead of an email)."""
    token = request.query_params.get("token", "")
    return {
        "request": request,
        "active_page": active_page,
        "token": token,
        "owner_link": f"{request.url.scheme}://{request.url.netloc}/?owner={token}",
        "owner_email": None,
        "owner_initial": None,
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request,
                          _: None = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "dashboard")
    ctx.update({
        "usage": store.usage_summary(),
        "active": security.active_count(),
        "turnstile_on": bool(config.TURNSTILE_SECRET_KEY),
        "limits": {
            "Sessions per IP per day": config.SESSIONS_PER_IP_PER_DAY,
            "Sessions per visitor per day": config.SESSIONS_PER_VISITOR_PER_DAY,
            "Global sessions per day": config.GLOBAL_SESSIONS_PER_DAY,
            "Global STT minutes per day": config.GLOBAL_STT_SECONDS_PER_DAY // 60,
            "Max session seconds": config.SESSION_MAX_SECONDS,
            "Max concurrent calls": config.MAX_CONCURRENT_SESSIONS,
        },
    })
    return templates.TemplateResponse("admin_dashboard.html", ctx)


@app.get("/admin/leads", response_class=HTMLResponse)
async def admin_leads_page(request: Request,
                           _: None = Depends(_require_admin)) -> Response:
    leads_out = []
    for lead in store.list_leads(100):
        lead = dict(lead)
        lead["when"] = datetime.datetime.fromtimestamp(
            lead["created_at"], tz=datetime.timezone.utc
        ).strftime("%b %d, %H:%M")
        leads_out.append(lead)

    ctx = _admin_base_context(request, "leads")
    ctx.update({"leads": leads_out, "sheets_url": config.SHEETS_URL})
    return templates.TemplateResponse("admin_leads.html", ctx)


@app.get("/admin/knowledge", response_class=HTMLResponse)
async def admin_knowledge_page(request: Request,
                               _: None = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "knowledge")
    ctx.update({"kb": KB.stats(), "kb_files": _knowledge_file_status()})
    return templates.TemplateResponse("admin_knowledge.html", ctx)


@app.get("/admin/integrations", response_class=HTMLResponse)
async def admin_integrations_page(request: Request,
                                  _: None = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "integrations")
    return templates.TemplateResponse("admin_integrations.html", ctx)


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request,
                              _: None = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "settings")
    ctx.update({
        "agent_rules": prompts.active_behavior_rules(),
        "default_rules": prompts._default_behavior_rules(),
    })
    return templates.TemplateResponse("admin_settings.html", ctx)


@app.post("/admin/settings")
async def save_admin_settings(request: Request,
                              _: None = Depends(_require_admin)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    rules = body.get("agent_rules")
    if rules is None or not isinstance(rules, str):
        return JSONResponse({"error": "agent_rules must be a string."}, status_code=400)
    if len(rules) > 8000:
        return JSONResponse({"error": "That's too long - keep it under 8000 characters."},
                            status_code=400)

    store.set_setting(prompts.SETTINGS_KEY_AGENT_RULES, rules.strip())
    log.info("admin updated agent behaviour rules (%d chars)", len(rules))
    return JSONResponse({"ok": True})


@app.post("/admin/knowledge")
async def upload_knowledge(request: Request, file: UploadFile = File(...),
                           target: Optional[str] = Form(None),
                           _: None = Depends(_require_admin)) -> Response:
    if target:
        canonical = KNOWLEDGE_TARGETS.get(target)
        if not canonical:
            return JSONResponse({"error": "Unknown target."}, status_code=400)
        name = canonical
    else:
        name = (file.filename or "").strip().replace("/", "_").replace("\\", "_")
        if not name.endswith(".md"):
            return JSONResponse({"error": "Only .md files are accepted."}, status_code=400)

    content = (await file.read()).decode("utf-8", errors="replace")
    config.KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    (config.KNOWLEDGE_DIR / name).write_text(content, encoding="utf-8")
    KB.load()
    return JSONResponse({"ok": True, "kb": KB.stats()})


@app.post("/admin/knowledge/reload")
async def reload_knowledge(request: Request,
                           _: None = Depends(_require_admin)) -> Response:
    KB.load()
    return JSONResponse({"ok": True, "kb": KB.stats()})


@app.post("/admin/leads/delete")
async def delete_leads(request: Request,
                       _: None = Depends(_require_admin)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    if body.get("all"):
        removed = store.delete_all_leads()
        log.info("admin deleted all leads (%d rows)", removed)
        return JSONResponse({"ok": True, "removed": removed})

    ids = body.get("ids") or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return JSONResponse({"error": "ids must be a list of integers."}, status_code=400)
    if not ids:
        return JSONResponse({"error": "No lead ids given."}, status_code=400)

    removed = store.delete_leads(ids)
    log.info("admin deleted %d lead(s)", removed)
    return JSONResponse({"ok": True, "removed": removed})
