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
import csv
import datetime
import io
import logging
import re
import secrets
import time
import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import (
    Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile, File,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    branding, config, fillers, integrations, knowledge, leads, llm, mailer,
    prompts, security, stt, store, tts, voices,
)

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
    default_kb = knowledge.get_kb(None)
    missing = integrations.missing_required()
    if missing:
        log.error("MISSING REQUIRED ENV VARS: %s - the agent will refuse calls",
                  ", ".join(missing))
    if not config.ALLOWED_ORIGINS:
        log.warning("ALLOWED_ORIGINS is empty - set it to your domain before "
                    "going public, otherwise any site can embed this agent.")
    log.info("knowledge base: %s", default_kb.stats())
    log.info("approved sub-accounts: %d/%d", store.count_approved_accounts(),
             config.MAX_ACCOUNTS)
    voice_ids = voices.all_voice_ids(integrations.fish_model_id())
    fillers.load_cached(voice_ids)
    if config.ENABLE_FILLERS and not missing:
        # Rendered in the background so the container reports healthy at once.
        asyncio.create_task(fillers.warm(voice_ids))


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


# --- Sub-account resolution ---------------------------------------------

def _resolve_account(request: Request, override: str = "") -> Optional[dict[str, Any]]:
    """None means the original single-tenant agent - the ?account=/subdomain
    mechanism is purely additive, so a request with neither behaves exactly
    as it always has.

    Subdomain resolution only activates once PLATFORM_ROOT_DOMAIN is set
    (i.e. once a real domain is bought and pointed here) - until then every
    account is still reachable via ?account=<slug>, which keeps working
    unchanged afterwards too.
    """
    slug = (override or request.query_params.get("account", "")).strip().lower()
    if not slug and config.PLATFORM_ROOT_DOMAIN:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        root = config.PLATFORM_ROOT_DOMAIN
        if host.endswith("." + root):
            sub = host[: -(len(root) + 1)]
            if sub and sub != "www":
                slug = sub
    if not slug:
        return None
    return store.get_account_by_slug(slug)


LIMIT_EMAIL_COOLDOWN_SECONDS = 20 * 3600


async def _maybe_notify_limit(account: dict[str, Any], reason: str) -> None:
    """Fire-and-forget email to the business owner when their agent hits a
    cap - throttled so a burst of denied calls sends one email, not one per
    attempt."""
    last = account.get("last_limit_email_at")
    if last and (time.time() - last) < LIMIT_EMAIL_COOLDOWN_SECONDS:
        return
    store.mark_limit_email_sent(account["id"])
    with contextlib.suppress(Exception):
        await mailer.send_limit_reached(account["email"], account["business_name"], reason)


# --- Pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    visitor_id, is_new = _visitor_id_from(request)
    account = _resolve_account(request)
    account_id = account["id"] if account else None
    current_branding = branding.get_branding(account_id)
    # Set only on the iframe embed.js creates - Turnstile does not work
    # reliably inside a cross-origin iframe on a third-party site, so this
    # path skips even loading Cloudflare's script rather than let it fail
    # there every time. See embed.js/app.js for the full rationale.
    is_embed = request.query_params.get("embed") == "1"
    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "brand": current_branding["brand_name"],
            "tagline": current_branding["tagline"],
            "agent_name": current_branding["agent_name"],
            "turnstile_site_key": None if is_embed else integrations.turnstile_site_key(),
            "logo_url": branding.logo_url(account_id, "logo"),
            "favicon_url": branding.logo_url(account_id, "favicon"),
            "apple_touch_icon_url": branding.logo_url(account_id, "apple_touch_icon"),
        },
    )
    if is_new:
        _set_visitor_cookie(response, visitor_id)
    return response


@app.get("/branding/{scope}/{filename}")
async def branding_asset(scope: str, filename: str) -> Response:
    """Serves per-account logo/favicon/apple-touch-icon (see branding.py).
    scope is either "default" (the main account) or an account id."""
    kind_by_filename = {"logo.png": "logo", "favicon.png": "favicon",
                        "apple-touch-icon.png": "apple_touch_icon"}
    kind = kind_by_filename.get(filename)
    if not kind:
        raise HTTPException(status_code=404)

    account_id: Optional[int] = None
    if scope != "default":
        if not scope.isdigit():
            raise HTTPException(status_code=404)
        account_id = int(scope)

    path = branding.logo_paths(account_id)[kind]
    if not path.exists():
        path = branding.logo_paths(None)[kind]
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=300"})


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


# --- Session issuing --------------------------------------------------------

@app.post("/api/session/start")
async def start_session(request: Request) -> Response:
    if integrations.missing_required():
        return JSONResponse(
            {"error": "The agent is not configured yet."}, status_code=503
        )

    origin = request.headers.get("origin", "")
    if origin and not security.origin_allowed(origin):
        return JSONResponse({"error": "Not allowed from this site."}, status_code=403)

    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    requested_slug = (body.get("account") or "").strip().lower()
    account = _resolve_account(request, requested_slug)
    if requested_slug and account is None:
        return JSONResponse({"error": "This voice agent is not available."},
                            status_code=404)
    account_id = account["id"] if account else None

    ip = security.client_ip(request)
    ip_hash = security.hash_ip(ip)
    visitor_id, is_new = _visitor_id_from(request)
    scoped_visitor_id = store.visitor_key(visitor_id, account_id)

    # The owner testing their own deployed site would otherwise trip the same
    # per-IP daily cap as the public - incognito clears the visitor cookie but
    # not the IP. A token only the owner has (never shipped in the client
    # bundle - see static/app.js) skips both the bot check and the quotas.
    #
    # A sub-account admin gets the same bypass on THEIR OWN agent for free
    # just by being logged into their own dashboard in the same browser (the
    # admin session cookie already proves who they are) - without this, a
    # business owner testing their own agent from their own dashboard link
    # was hitting the same public anti-abuse quotas as a stranger, which is
    # exactly the "usage is over" report this fixes.
    admin_ctx = _admin_context_from_cookie(request)
    is_owner = (
        security.is_admin(request.headers.get("x-owner-token", ""))
        or bool(admin_ctx and (admin_ctx["is_super"] or admin_ctx["account_id"] == account_id))
    )
    # Set only by requests from embed.js's iframe (see there for why): skips
    # Turnstile specifically, since it does not work reliably inside a
    # cross-origin iframe on a third-party site and there is no way to
    # pre-register every future customer's embedding domain with it. Unlike
    # is_owner, this does NOT skip the quota/account checks below - those
    # are the actual abuse protection for this path.
    is_embed = bool(body.get("embed"))

    if not is_owner and not is_embed and not await security.verify_turnstile(
        body.get("turnstile_token", ""), ip
    ):
        return JSONResponse(
            {"error": "We could not verify you are human. Please reload and retry."},
            status_code=403,
        )

    if not is_owner:
        try:
            security.check_account(account)
            security.check_account_daily_cap(account)
            security.enforce_quotas(ip_hash, scoped_visitor_id, account_id)
        except security.Denied as denied:
            log.info("session denied (%s) ip=%s account=%s", denied.reason,
                     ip_hash[:8], account_id)
            if account and denied.reason in ("trial_expired", "account_daily_cap"):
                asyncio.create_task(_maybe_notify_limit(account, denied.reason))
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

    store.touch_visitor(scoped_visitor_id)
    store.create_session(session_id, scoped_visitor_id, ip_hash, account_id)

    payload = {
        "session_id": session_id,
        "session_token": security.sign(
            {"sid": session_id, "vid": scoped_visitor_id, "aid": account_id},
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

    def __init__(self, ws: WebSocket, session_id: str, visitor_id: str,
                account_id: Optional[int] = None) -> None:
        self.ws = ws
        self.session_id = session_id
        self.visitor_id = visitor_id
        self.account_id = account_id
        # Resolved once here rather than looked up per-turn: None (a
        # sub-account on the "Indian English (Male)" preset, or the main
        # account) means "the owner's own configured voice", so this is
        # always a concrete Fish Audio id from here on.
        self.voice_id = voices.account_fish_model_id(account_id) or integrations.fish_model_id()
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

        # Lead capture: the LLM decides it is time and signals it with a
        # marker (see prompts.CONTACT_MARKER); the backend then takes over,
        # asking for name, email and phone one at a time in plain speech -
        # not a form, so there is no fixed-size box to squeeze a multi-field
        # layout into. The visual form only ever appears afterwards, and only
        # if the caller says the spoken-back summary is wrong.
        self.awaiting_contact = False
        # "name" | "email" | "phone" | "confirm" | None - which question the
        # caller's next reply answers.
        self.contact_step: Optional[str] = None
        self.contact_step_attempts = 0
        self.contact_phone_asked = False
        self.contact_fields: dict[str, Optional[str]] = {
            "name": None, "email": None, "phone": None,
        }
        # Once True, the marker is ignored for the rest of the call - covers
        # confirm, skip, and decline, so the model asking again cannot reopen
        # collection after the caller has already answered.
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


def _greeting_for(visitor: dict[str, Any], account_id: Optional[int] = None) -> str:
    current_branding = branding.get_branding(account_id)
    name = visitor.get("name")
    if visitor.get("session_count", 0) > 1 and name:
        return (f"Hi {name}, welcome back to {current_branding['brand_name']}. "
                "What can I help you with today?")
    if visitor.get("session_count", 0) > 1:
        return (f"Welcome back to {current_branding['brand_name']}. "
                "What can I help you with today?")
    return current_branding["greeting"]


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
    account_id = claims.get("aid")

    await ws.accept()
    call = Call(ws, session_id, visitor_id, account_id)
    log.info("call %s started (visitor %s, account %s)", session_id[:8],
             visitor_id[:8], account_id)

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
                         call.contact_confirmed_data, call.account_id)
            )
        log.info("call %s ended after %.0fs, %d turns",
                 session_id[:8], call.elapsed, call.turn_count)


async def _finalise(session_id: str, visitor_id: str,
                    history: list[dict[str, str]],
                    confirmed_contact: Optional[dict[str, Any]] = None,
                    account_id: Optional[int] = None) -> None:
    with contextlib.suppress(Exception):
        await leads.process_session(session_id, visitor_id, history,
                                    confirmed_contact, account_id)


async def _watchdog(call: Call) -> None:
    """Hard hangup at the session limit, regardless of what the client does."""
    try:
        await asyncio.sleep(config.SESSION_MAX_SECONDS)
        await call.send_json({
            "type": "ended",
            "reason": "time_limit",
            "message": "That is all the time this demo allows. "
                       f"Visit {branding.website_url(call.account_id)} to keep the conversation going.",
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
                           f"Visit {branding.website_url(call.account_id)} any time.",
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
        clip = fillers.bridge(call.voice_id, integrations.fish_model_id())
        if clip:
            await call.send_audio(clip, real=False)
    except asyncio.CancelledError:
        raise


async def _handle_greeting(call: Call) -> None:
    text = _greeting_for(call.visitor, call.account_id)
    call.history.append({"role": "assistant", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "assistant", text, call.account_id)
    await _speak(call, _once(text), announce=text)


async def _handle_turn(call: Call, text: str) -> None:
    if call.turn_count >= config.MAX_TURNS_PER_SESSION:
        await call.send_json({
            "type": "ended", "reason": "turn_limit",
            "message": f"We have covered a lot. Visit {branding.website_url(call.account_id)} "
                       "and the team will take it from here.",
        })
        return

    # No stop_speaking() here: this coroutine *is* call.turn, so cancelling
    # would cancel itself. _start_turn already cleared the previous turn.
    call.cancel = asyncio.Event()

    call.history.append({"role": "user", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "user", text, call.account_id)
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
    if config.ENABLE_FILLERS and fillers.ready(call.voice_id, integrations.fish_model_id()):
        call.audio_started.clear()
        clip = fillers.ack(call.voice_id, integrations.fish_model_id())
        if clip:
            await call.send_json({"type": "agent_start"})
            await call.send_audio(clip, real=False)
            bridge_task = asyncio.create_task(_bridge_if_slow(call))

    system = prompts.build_system_prompt(
        text, call.visitor, _history_hint(call.visitor_id), call.account_id
    )
    if call.contact_flow_done:
        system += (
            "\n\nIMPORTANT: this caller's name, email and phone have already "
            "been collected (or they declined) earlier in THIS call. Do not "
            "ask for any contact detail again, do not say anything like "
            "'could I get your name/email/phone', and do not emit "
            f"{prompts.CONTACT_MARKER} again. Continue the conversation "
            "naturally as its own next turn - do not reintroduce yourself, "
            "greet them, or restate who you are or what the company does as "
            "if this were the start of a new call."
        )
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
        store.add_turn(call.session_id, call.visitor_id, "assistant", spoken, call.account_id)

    if marker_flag["seen"] and not call.contact_flow_done:
        await _begin_contact_flow(call)
    elif marker_flag["seen"] and call.contact_flow_done:
        # The model tried to re-open contact capture despite the instruction
        # above and despite already being told not to in the fixed prefix.
        # The marker itself is stripped before TTS either way, but whatever
        # sentence the model spoke immediately before it (e.g. "could I get
        # your email") already went out over the live audio stream and cannot
        # be un-said. Logged so a repeat-itself report can be confirmed
        # against real call logs instead of guessed at.
        log.warning("call %s: model re-emitted CONTACT_MARKER after "
                    "contact_flow_done - spoken reply may have re-asked for "
                    "contact details", call.session_id[:8])


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


CONTACT_FIELD_PROMPTS = {
    "name": "Sure, could I get your name?",
    "email": "And what's the best email to reach you?",
    "phone": "And a phone number, if you're happy to share one? You can also just say skip.",
}
CONTACT_FIELD_ORDER = ("name", "email", "phone")


async def _say_contact_line(call: Call, text: str) -> None:
    """A fixed backend-spoken line during contact capture - not an LLM reply,
    so it is exact, never wanders, and never re-emits the marker."""
    call.history.append({"role": "assistant", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "assistant", text, call.account_id)
    await _speak(call, _once(text), announce=text)


async def _begin_contact_flow(call: Call) -> None:
    """Entered once, right after the model decides it is time to collect
    contact details. Everything from here on is asked one field at a time in
    plain speech, in the same transcript area as the rest of the call - not a
    form, so there is no multi-field layout to fit into a fixed-size box."""
    call.awaiting_contact = True
    await _advance_contact_flow(call)


async def _advance_contact_flow(call: Call) -> None:
    """Ask whichever of name/email/phone is still missing, in order, or move
    to reading the details back once name and email are in (phone stays
    optional, but is still asked for once before moving on)."""
    for field in ("name", "email"):
        if not call.contact_fields.get(field):
            await _ask_contact_field(call, field)
            return
    if not call.contact_fields.get("phone") and not call.contact_phone_asked:
        await _ask_contact_field(call, "phone")
        return
    await _ask_contact_confirmation(call)


async def _ask_contact_field(call: Call, field: str) -> None:
    call.contact_step = field
    if field == "phone":
        call.contact_phone_asked = True
    await _say_contact_line(call, CONTACT_FIELD_PROMPTS[field])


def _contact_summary(call: Call) -> str:
    parts = []
    for field, label in (("name", "name"), ("email", "email"), ("phone", "phone")):
        if call.contact_fields.get(field):
            parts.append(f"{label} as {call.contact_fields[field]}")
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


async def _ask_contact_confirmation(call: Call) -> None:
    call.contact_step = "confirm"
    text = f"Let me read that back - I have your {_contact_summary(call)}. Is that all correct?"
    await _say_contact_line(call, text)


async def _show_contact_form(call: Call) -> None:
    """Falls back to the visual editable form - only reached if the spoken
    flow could not get a clean answer, or the caller said the read-back
    summary was wrong. Pre-filled with whatever was already collected."""
    call.contact_step = None
    await call.send_json({"type": "show_contact_form", **call.contact_fields})


async def _finalise_contact(call: Call) -> None:
    call.contact_confirmed_data = dict(call.contact_fields)
    call.awaiting_contact = False
    call.contact_flow_done = True
    call.contact_step = None

    transcript = leads.transcript_text(call.history)
    lead_id = store.save_lead(
        call.session_id, call.visitor_id, call.contact_fields, transcript,
        call.account_id,
    )
    store.update_visitor_memory(
        call.visitor_id, name=call.contact_fields.get("name"),
        email=call.contact_fields.get("email"), phone=call.contact_fields.get("phone"),
    )
    log.info("lead %s confirmed live for session %s", lead_id, call.session_id[:8])

    await call.send_json({"type": "contact_saved"})
    await _say_contact_line(
        call,
        "Thank you, that is saved. Our team will reach out soon. Is there "
        "anything else I can help with?",
    )


async def _handle_contact_reply(call: Call, text: str) -> None:
    """Routes the caller's next turn to whichever step of contact capture is
    currently active - a single field, or the final yes/no confirmation."""
    if call.contact_step == "confirm":
        await _handle_contact_confirmation_reply(call, text)
    else:
        await _handle_contact_field_reply(call, text)


async def _handle_contact_field_reply(call: Call, text: str) -> None:
    field = call.contact_step
    data = await leads.extract_contact_reply(text)

    if data.get("declined"):
        if field == "phone":
            # Only phone is optional enough to just skip past on a decline;
            # declining name or email ends the whole flow, as before.
            call.contact_phone_asked = True
            call.contact_step_attempts = 0
            await _advance_contact_flow(call)
            return
        call.awaiting_contact = False
        call.contact_flow_done = True
        call.contact_step = None
        await _respond_with_llm(call, text)
        return

    changed = False
    for key in ("name", "email", "phone"):
        if data.get(key):
            call.contact_fields[key] = data[key]
            changed = True
    if changed:
        await call.send_json({"type": "contact_fields", **call.contact_fields})

    if not call.contact_fields.get(field) and field != "phone":
        # Didn't catch it - ask the same question again, once. A second miss
        # hands off to the visual form so the caller can just type it rather
        # than get stuck repeating themselves to a machine.
        if call.contact_step_attempts >= 1:
            await _show_contact_form(call)
            return
        call.contact_step_attempts += 1
        await _ask_contact_field(call, field)
        return

    call.contact_step_attempts = 0
    await _advance_contact_flow(call)


async def _handle_contact_confirmation_reply(call: Call, text: str) -> None:
    data = await leads.extract_confirmation_reply(text)

    corrected = False
    for key in ("name", "email", "phone"):
        if data.get(key):
            call.contact_fields[key] = data[key]
            corrected = True
    if corrected:
        await call.send_json({"type": "contact_fields", **call.contact_fields})

    if data.get("confirmed"):
        await _finalise_contact(call)
        return

    if corrected:
        # They corrected something inline rather than just saying yes/no -
        # read the updated version back rather than assuming that means done.
        await _ask_contact_confirmation(call)
        return

    # Said no, or gave nothing usable - hand off to the visual form.
    await _show_contact_form(call)


async def _handle_contact_confirmed(call: Call, message: dict[str, Any]) -> None:
    """The caller edited/reviewed the fallback form and clicked Confirm."""
    name = (message.get("name") or "").strip()[:120] or None
    email = (message.get("email") or "").strip().lower()[:200] or None
    phone = (message.get("phone") or "").strip()[:40] or None
    call.contact_fields = {"name": name, "email": email, "phone": phone}
    await _finalise_contact(call)


async def _handle_contact_skip(call: Call) -> None:
    """The caller clicked 'not now' on the fallback form."""
    call.awaiting_contact = False
    call.contact_flow_done = True
    call.contact_step = None
    await call.send_json({"type": "hide_contact_form"})

    text = "No thanks, not right now."
    call.history.append({"role": "user", "content": text})
    store.add_turn(call.session_id, call.visitor_id, "user", text, call.account_id)
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
        return await tts.speak(relay(clauses), call.send_audio, call.cancel,
                               voice_id=call.voice_id)

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
# Generous for markdown text (way beyond KB_INLINE_LIMIT's ~7000 characters)
# while ruling out an authenticated account uploading something huge enough
# to pressure disk/memory on a VPS every other account also runs on.
MAX_KNOWLEDGE_FILE_BYTES = 2 * 1024 * 1024


ADMIN_SESSION_COOKIE = "im_admin_session"
ADMIN_SESSION_SECONDS = 30 * 24 * 3600

# The owner's own booking link (Cal.com, Google Calendar, etc.), shown as
# "Book a 1:1 call" in the support widget sub-account admins see. Global -
# there is one owner to book time with, not one per sub-account.
SETTINGS_KEY_CALENDAR_LINK = "calendar_link"


def _admin_context_from_cookie(request: Request) -> Optional[dict[str, Any]]:
    """Resolves a magic-link session cookie to who is logged in and what
    they can see: the main-account owner (is_super, sees everything, no
    account_id gate) or one sub-account's contact - re-checked against the
    accounts table on every request (not just baked into the cookie once),
    so deleting or rejecting an account revokes an already-issued session
    immediately rather than after it expires."""
    raw = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if not raw:
        return None
    claims = security.verify(raw)
    if not claims:
        return None
    email = claims.get("email", "")
    if email in config.ADMIN_EMAILS:
        return {"is_super": True, "account_id": None, "email": email}
    account_id = claims.get("account_id")
    if account_id is None:
        return None
    account = store.get_account(account_id)
    if not account or account["status"] != "approved" or account["email"] != email:
        return None
    return {"is_super": False, "account_id": account_id, "email": email,
            "account": account}


def _require_admin(request: Request) -> dict[str, Any]:
    """Accepts EITHER the standing ?token= (a permanent "master key" useful
    for the owner's own API/automation access, always the main super account)
    OR a valid magic-link session cookie (main account or one sub-account).
    A plain browser page load (GET) that fails both is sent to the login page
    instead of a bare 404, since a human looking at that URL can actually do
    something about it; an API call (POST) still just gets 404, same as
    before - it was never meant to be user-facing."""
    token = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    if security.is_admin(token):
        return {"is_super": True, "account_id": None, "email": None}
    ctx = _admin_context_from_cookie(request)
    if ctx:
        return ctx
    if request.method == "GET":
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    raise HTTPException(status_code=404, detail="Not found")


def _require_super(admin: dict[str, Any] = Depends(_require_admin)) -> dict[str, Any]:
    """Gates the pages/actions that stay owner-only: integrations, global
    agent behaviour, and the accounts queue itself. A sub-account admin
    hitting one of these gets the same 404 as a stranger - it should not even
    reveal that the page exists."""
    if not admin["is_super"]:
        raise HTTPException(status_code=404, detail="Not found")
    return admin


def _knowledge_file_status(account_id: Optional[int] = None) -> dict[str, dict[str, Any]]:
    status = {}
    directory = knowledge.dir_for(account_id)
    for key, filename in KNOWLEDGE_TARGETS.items():
        path = directory / filename
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


def _admin_base_context(request: Request, active_page: str,
                        admin: dict[str, Any]) -> dict[str, Any]:
    """Shared context every admin page needs - the sidebar, the theme toggle,
    and whatever identifies the logged-in admin. Falls back to showing
    "token access" instead of an email for anyone in via the standing
    ?token= master key rather than a real magic-link login."""
    token = request.query_params.get("token", "")
    email = admin.get("email")
    account = admin.get("account")
    owner_link = f"{request.url.scheme}://{request.url.netloc}/?owner={config.ADMIN_TOKEN}"
    if account:
        owner_link = (f"{request.url.scheme}://{request.url.netloc}/"
                     f"?account={account['slug']}")
    return {
        "request": request,
        "active_page": active_page,
        "token": token,
        "is_super": admin["is_super"],
        "account": account,
        # The actual master key, not whatever happens to be in this page's
        # URL - a magic-link login never has ?token= in the address bar, so
        # reading it from query params left this blank for anyone who logged
        # in that way.
        "owner_link": owner_link,
        "owner_email": email,
        "owner_initial": email[0].upper() if email else None,
        "calendar_link": store.get_setting(SETTINGS_KEY_CALENDAR_LINK),
    }


def _email_may_login(email: str) -> bool:
    """A sub-account's contact email doubles as its admin login, same as
    ADMIN_EMAILS does for the main account - no separate password to manage,
    and it is revoked automatically the moment the account stops being
    'approved' (see _admin_context_from_cookie)."""
    if email in config.ADMIN_EMAILS:
        return True
    account = store.get_account_by_email(email)
    return bool(account and account["status"] == "approved")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request) -> Response:
    if _admin_context_from_cookie(request):
        return RedirectResponse("/admin")
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.post("/admin/login")
async def admin_login_request(request: Request) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    email = (body.get("email") or "").strip().lower()

    # Cooldown is checked (and always recorded) regardless of whether this
    # email is even eligible to log in, and the response never reveals
    # whether it fired - a distinguishable response either way would let
    # someone email-bomb an address, or use timing/response differences to
    # probe which addresses are valid admins.
    on_cooldown = False
    if email:
        try:
            security.check_cooldown(f"login:{email}", 30)
        except security.Denied:
            on_cooldown = True

    if email and not on_cooldown and _email_may_login(email):
        if not config.BREVO_API_KEY or not config.BREVO_SENDER_EMAIL:
            log.error("magic-link login requested but Brevo is not configured")
            return JSONResponse(
                {"error": "Login email is not configured yet. Use the token link instead."},
                status_code=503,
            )
        login_token = secrets.token_urlsafe(32)
        store.create_login_token(login_token, email)
        link = f"{request.url.scheme}://{request.url.netloc}/admin/login/verify?token={login_token}"
        try:
            await mailer.send_login_link(email, link)
        except Exception as exc:
            log.error("failed to send login email to %s...: %s", email[:3], exc)
            return JSONResponse(
                {"error": "Could not send the email right now. Try again shortly."},
                status_code=502,
            )
        log.info("magic-link sent to %s...", email[:3])
    return JSONResponse({"ok": True})


@app.get("/admin/login/verify")
async def admin_login_verify(request: Request, token: str = "") -> Response:
    email = store.consume_login_token(token)
    if not email:
        return RedirectResponse("/admin/login?error=1")

    claims: dict[str, Any] = {"email": email}
    if email not in config.ADMIN_EMAILS:
        account = store.get_account_by_email(email)
        if not account or account["status"] != "approved":
            return RedirectResponse("/admin/login?error=1")
        claims["account_id"] = account["id"]

    response = RedirectResponse("/admin")
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        security.sign(claims, ADMIN_SESSION_SECONDS),
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    log.info("admin login: %s...", email[:3])
    return response


@app.post("/admin/logout")
async def admin_logout() -> Response:
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request,
                          admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "dashboard", admin)
    ctx["usage"] = store.usage_summary(admin["account_id"])
    base = f"{request.url.scheme}://{request.url.netloc}"
    if admin["is_super"]:
        # No data-account attribute at all - embed.js treats that as "the
        # main account", same as the direct link needing no ?account=.
        ctx.update({
            "direct_link": base + "/",
            "embed_snippet": f'<script src="{base}/embed.js" async><' + '/script>',
        })
    elif admin.get("account"):
        slug = admin["account"]["slug"]
        ctx.update({
            "direct_link": f"{base}/?account={slug}",
            "embed_snippet": f'<script src="{base}/embed.js" data-account="{slug}" async><' + '/script>',
        })
    if admin["is_super"]:
        ctx.update({
            "active": security.active_count(),
            "turnstile_on": bool(integrations.turnstile_secret_key()),
            "accounts_pending": len(store.list_accounts("pending")),
            "accounts_approved": store.count_approved_accounts(),
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
                           admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    leads_out = []
    for lead in store.list_leads(100, admin["account_id"]):
        lead = dict(lead)
        lead["when"] = datetime.datetime.fromtimestamp(
            lead["created_at"], tz=datetime.timezone.utc
        ).strftime("%b %d, %H:%M")
        leads_out.append(lead)

    ctx = _admin_base_context(request, "leads", admin)
    ctx["leads"] = leads_out
    return templates.TemplateResponse("admin_leads.html", ctx)


@app.get("/admin/knowledge", response_class=HTMLResponse)
async def admin_knowledge_page(request: Request,
                               admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "knowledge", admin)
    ctx.update({
        "kb": knowledge.get_kb(admin["account_id"]).stats(),
        "kb_files": _knowledge_file_status(admin["account_id"]),
    })
    return templates.TemplateResponse("admin_knowledge.html", ctx)


def _mask_secret(value: str) -> str:
    """Never round-trip a real secret into the HTML source. The form field
    itself stays blank (blank on save means "leave unchanged", same as the
    branding fields); this is only for the "currently set, ending in ...XXXX"
    hint shown below it."""
    if not value:
        return ""
    return value[-4:] if len(value) > 4 else "••"


@app.get("/admin/integrations", response_class=HTMLResponse)
async def admin_integrations_page(request: Request,
                                  admin: dict[str, Any] = Depends(_require_super)) -> Response:
    current = integrations.get_integrations()
    ctx = _admin_base_context(request, "integrations", admin)
    ctx.update({
        "models": {
            "pollinations_model": current["pollinations_model"],
            "fish_model": current["fish_model"],
            "aai_speech_model": current["aai_speech_model"],
        },
        # Non-secret - a voice ID and a public site key, safe to show plainly.
        "fish_model_id": current["fish_model_id"],
        "turnstile_site_key": current["turnstile_site_key"],
        # Secret - masked, see _mask_secret above.
        "assemblyai_key_hint": _mask_secret(current["assemblyai_api_key"]),
        "pollinations_key_hint": _mask_secret(current["pollinations_api_key"]),
        "fish_key_hint": _mask_secret(current["fish_api_key"]),
        "turnstile_secret_hint": _mask_secret(current["turnstile_secret_key"]),
    })
    return templates.TemplateResponse("admin_integrations.html", ctx)


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request,
                              admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    ctx = _admin_base_context(request, "settings", admin)
    ctx.update({
        "agent_rules": prompts.active_behavior_rules(admin["account_id"]),
        "default_rules": prompts._default_behavior_rules(admin["account_id"]),
        "branding": branding.get_branding(admin["account_id"]),
        "logo_url": branding.logo_url(admin["account_id"], "logo"),
    })
    if admin["account_id"] is not None:
        ctx.update({
            "voice_presets": [
                {"key": key, "label": voices.VOICE_PRESETS[key]["label"]}
                for key in voices.VOICE_PRESET_ORDER
            ],
            "current_voice_preset": voices.account_voice_preset(admin["account_id"]),
        })
    return templates.TemplateResponse("admin_settings.html", ctx)


@app.post("/admin/settings/voice")
async def save_voice_preset(request: Request,
                            admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    if admin["account_id"] is None:
        return JSONResponse(
            {"error": "The main account's voice is set from Integrations."},
            status_code=400,
        )
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    preset = (body.get("voice_preset") or "").strip()
    if preset not in voices.VOICE_PRESETS:
        return JSONResponse({"error": "Unknown voice."}, status_code=400)
    store.set_setting(voices.SETTINGS_KEY_VOICE_PRESET, preset, admin["account_id"])
    log.info("account %s voice preset set to %s", admin["account_id"], preset)
    return JSONResponse({"ok": True})


@app.post("/admin/settings")
async def save_admin_settings(request: Request,
                              admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    rules = body.get("agent_rules")
    if rules is None or not isinstance(rules, str):
        return JSONResponse({"error": "agent_rules must be a string."}, status_code=400)
    if len(rules) > 8000:
        return JSONResponse({"error": "That's too long - keep it under 8000 characters."},
                            status_code=400)

    store.set_setting(prompts.SETTINGS_KEY_AGENT_RULES, rules.strip(), admin["account_id"])
    log.info("admin updated agent behaviour rules (%d chars, account=%s)",
             len(rules), admin["account_id"])
    return JSONResponse({"ok": True})


BRANDING_TEXT_FIELDS = {
    "brand_name": (branding.SETTINGS_KEY_BRAND_NAME, 120),
    "tagline": (branding.SETTINGS_KEY_TAGLINE, 120),
    "agent_name": (branding.SETTINGS_KEY_AGENT_NAME, 120),
    "greeting": (branding.SETTINGS_KEY_GREETING, 500),
}
MAX_LOGO_BYTES = 5 * 1024 * 1024


@app.post("/admin/settings/branding")
async def save_branding(request: Request,
                        brand_name: str = Form(""),
                        tagline: str = Form(""),
                        agent_name: str = Form(""),
                        greeting: str = Form(""),
                        logo: Optional[UploadFile] = File(None),
                        admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    for field_name, value in (("brand_name", brand_name), ("tagline", tagline),
                              ("agent_name", agent_name), ("greeting", greeting)):
        limit = BRANDING_TEXT_FIELDS[field_name][1]
        if len(value) > limit:
            return JSONResponse(
                {"error": f"{field_name} is too long - keep it under {limit} characters."},
                status_code=400,
            )

    if logo is not None and logo.filename:
        content = await logo.read()
        if len(content) > MAX_LOGO_BYTES:
            return JSONResponse({"error": "Logo image is too large (5MB max)."},
                                status_code=400)
        try:
            branding.regenerate_logo_assets(content, admin["account_id"])
        except Exception as exc:
            log.warning("logo upload rejected: %s", exc)
            return JSONResponse({"error": "That does not look like a valid image."},
                                status_code=400)

    for field_name, value in (("brand_name", brand_name), ("tagline", tagline),
                              ("agent_name", agent_name), ("greeting", greeting)):
        stripped = value.strip()
        if stripped:
            store.set_setting(BRANDING_TEXT_FIELDS[field_name][0], stripped, admin["account_id"])

    log.info("admin updated branding (account=%s)", admin["account_id"])
    return JSONResponse({"ok": True, "branding": branding.get_branding(admin["account_id"])})


MODEL_SETTINGS_FIELDS = {
    "pollinations_model": integrations.SETTINGS_KEY_POLLINATIONS_MODEL,
    "fish_model": integrations.SETTINGS_KEY_FISH_MODEL,
    "aai_speech_model": integrations.SETTINGS_KEY_AAI_SPEECH_MODEL,
}


def _save_text_settings(body: dict[str, Any], fields: dict[str, str],
                        max_len: int = 200) -> Optional[JSONResponse]:
    """Shared save logic for the several small "a few text fields, blank
    means leave unchanged" forms on /admin/integrations. Returns an error
    response to return immediately, or None on success."""
    updates: dict[str, str] = {}
    for field_name, settings_key in fields.items():
        value = body.get(field_name)
        if value is None or not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) > max_len:
            return JSONResponse(
                {"error": f"{field_name} is too long - keep it under {max_len} characters."},
                status_code=400,
            )
        if value:
            updates[settings_key] = value
    for settings_key, value in updates.items():
        store.set_setting(settings_key, value)
    return None


@app.post("/admin/integrations/models")
async def save_model_settings(request: Request,
                              _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    error = _save_text_settings(body, MODEL_SETTINGS_FIELDS)
    if error:
        return error

    log.info("admin updated AI model settings")
    return JSONResponse({"ok": True, "models": integrations.get_models()})


PROVIDER_KEY_FIELDS = {
    "assemblyai_api_key": integrations.SETTINGS_KEY_ASSEMBLYAI_API_KEY,
    "pollinations_api_key": integrations.SETTINGS_KEY_POLLINATIONS_API_KEY,
    "fish_api_key": integrations.SETTINGS_KEY_FISH_API_KEY,
    "fish_model_id": integrations.SETTINGS_KEY_FISH_MODEL_ID,
}


@app.post("/admin/integrations/keys")
async def save_provider_keys(request: Request,
                             _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    error = _save_text_settings(body, PROVIDER_KEY_FIELDS, max_len=300)
    if error:
        return error

    log.info("admin updated provider API keys/voice ID")
    return JSONResponse({"ok": True})


TURNSTILE_FIELDS = {
    "turnstile_site_key": integrations.SETTINGS_KEY_TURNSTILE_SITE_KEY,
    "turnstile_secret_key": integrations.SETTINGS_KEY_TURNSTILE_SECRET_KEY,
}


@app.post("/admin/integrations/turnstile")
async def save_turnstile_settings(request: Request,
                                  _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    error = _save_text_settings(body, TURNSTILE_FIELDS, max_len=300)
    if error:
        return error

    log.info("admin updated Turnstile keys")
    return JSONResponse({"ok": True})


@app.post("/admin/knowledge")
async def upload_knowledge(request: Request, file: UploadFile = File(...),
                           target: Optional[str] = Form(None),
                           admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    if target:
        canonical = KNOWLEDGE_TARGETS.get(target)
        if not canonical:
            return JSONResponse({"error": "Unknown target."}, status_code=400)
        name = canonical
    else:
        name = (file.filename or "").strip().replace("/", "_").replace("\\", "_")
        if not name.endswith(".md"):
            return JSONResponse({"error": "Only .md files are accepted."}, status_code=400)

    raw = await file.read()
    if len(raw) > MAX_KNOWLEDGE_FILE_BYTES:
        return JSONResponse(
            {"error": f"That file is too large ({MAX_KNOWLEDGE_FILE_BYTES // 1024}KB max)."},
            status_code=400,
        )
    content = raw.decode("utf-8", errors="replace")
    directory = knowledge.dir_for(admin["account_id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")
    kb = knowledge.reload_kb(admin["account_id"])
    return JSONResponse({"ok": True, "kb": kb.stats()})


@app.post("/admin/knowledge/reload")
async def reload_knowledge(request: Request,
                           admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    kb = knowledge.reload_kb(admin["account_id"])
    return JSONResponse({"ok": True, "kb": kb.stats()})


@app.get("/admin/leads/export")
async def export_leads_csv(request: Request, ids: str = "",
                           admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    id_list: Optional[list[int]] = None
    if ids:
        try:
            id_list = [int(i) for i in ids.split(",") if i.strip()]
        except ValueError:
            return JSONResponse({"error": "Invalid ids."}, status_code=400)

    rows = store.list_leads_for_export(admin["account_id"], id_list)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "name", "email", "phone", "company", "problem",
                     "interest", "qualified", "created_at_utc"])
    for row in rows:
        writer.writerow([
            row["id"], row["name"] or "", row["email"] or "", row["phone"] or "",
            row["company"] or "", row["problem"] or "", row["interest"] or "",
            "yes" if row["qualified"] else "no",
            datetime.datetime.fromtimestamp(
                row["created_at"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


@app.post("/admin/leads/delete")
async def delete_leads(request: Request,
                       admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    if body.get("all"):
        removed = store.delete_all_leads(admin["account_id"])
        log.info("admin deleted all leads (%d rows, account=%s)", removed,
                 admin["account_id"])
        return JSONResponse({"ok": True, "removed": removed})

    ids = body.get("ids") or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return JSONResponse({"error": "ids must be a list of integers."}, status_code=400)
    if not ids:
        return JSONResponse({"error": "No lead ids given."}, status_code=400)

    removed = store.delete_leads(ids, admin["account_id"])
    log.info("admin deleted %d lead(s, account=%s)", removed, admin["account_id"])
    return JSONResponse({"ok": True, "removed": removed})


# --- Sub-account application + management -----------------------------

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_RESERVED_SLUGS = {"www", "admin", "api", "static", "app", "mail", "default"}


def _slugify(business_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")[:40] or "business"
    return base


def _ordinal_date(iso_date: str) -> str:
    """"2026-08-31" -> "31st August" - falls back to the raw string if it is
    not a plain ISO date, rather than raising on a malformed config value."""
    try:
        dt = datetime.datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return iso_date
    day = dt.day
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix} {dt.strftime('%B')}"


@app.get("/apply", response_class=HTMLResponse)
async def apply_page(request: Request) -> Response:
    spots_left = max(0, config.MAX_ACCOUNTS - store.count_approved_accounts())
    return templates.TemplateResponse(
        "apply.html",
        {
            "request": request,
            # config.BRAND_NAME, not branding.get_branding() - the marketing
            # page's own name should not silently follow whatever the owner
            # later renames their live agent's brand_name setting to.
            "brand": config.BRAND_NAME,
            "spots_left": spots_left,
            "trial_end": _ordinal_date(config.DEFAULT_TRIAL_END_DATE),
        },
    )


@app.post("/apply")
async def apply_submit(request: Request) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    business_name = (body.get("business_name") or "").strip()[:120]
    email = (body.get("email") or "").strip().lower()[:200]
    website = (body.get("website") or "").strip()[:200]
    note = (body.get("note") or "").strip()[:1000]
    country = (body.get("country") or "").strip()[:80]

    if website and not re.match(r"^https?://", website, re.IGNORECASE):
        website = f"https://{website}"

    if not business_name or not email or "@" not in email:
        return JSONResponse(
            {"error": "Business name and a valid email are required."}, status_code=400
        )
    if not website or "." not in website:
        return JSONResponse(
            {"error": "A website is required - callers are pointed there whenever "
                      "they hit a limit or the call ends."},
            status_code=400,
        )
    ip_hash = security.hash_ip(security.client_ip(request))
    try:
        security.check_cooldown(f"apply:{ip_hash}", 60)
    except security.Denied as denied:
        return JSONResponse({"error": denied.message}, status_code=denied.status)
    if store.count_approved_accounts() >= config.MAX_ACCOUNTS:
        return JSONResponse(
            {"error": "The beta is full right now. Check back soon."}, status_code=403
        )

    base_slug = _slugify(business_name)
    slug = base_slug
    for attempt in range(1, 20):
        if slug not in _RESERVED_SLUGS and _SLUG_RE.match(slug) and \
                not store.get_account_by_slug(slug):
            break
        slug = f"{base_slug}-{attempt + 1}"
    else:
        return JSONResponse({"error": "Could not allocate a slug. Try a different name."},
                            status_code=500)

    account_id = store.create_account_application(slug, business_name, email, website,
                                                   note, country)
    if account_id is None:
        return JSONResponse({"error": "Something went wrong. Please try again."},
                            status_code=500)
    log.info("new account application: %s (%s)", business_name, slug)
    return JSONResponse({"ok": True})


@app.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts_page(request: Request,
                              admin: dict[str, Any] = Depends(_require_super)) -> Response:
    ctx = _admin_base_context(request, "accounts", admin)
    accounts_out = []
    for account in store.list_accounts():
        account = dict(account)
        account["created"] = datetime.datetime.fromtimestamp(
            account["created_at"], tz=datetime.timezone.utc
        ).strftime("%b %d, %H:%M")
        if account.get("trial_ends_at"):
            ends_dt = datetime.datetime.fromtimestamp(
                account["trial_ends_at"], tz=datetime.timezone.utc
            )
            account["trial_ends"] = ends_dt.strftime("%b %d, %Y")
            account["trial_ends_iso"] = ends_dt.strftime("%Y-%m-%d")
        else:
            account["trial_ends_iso"] = ""
        accounts_out.append(account)
    ctx.update({
        "accounts": accounts_out,
        "max_accounts": config.MAX_ACCOUNTS,
        "approved_count": store.count_approved_accounts(),
        "default_trial_end": config.DEFAULT_TRIAL_END_DATE,
        "default_daily_minutes": config.DEFAULT_TRIAL_DAILY_SECONDS // 60,
        "root_domain": config.PLATFORM_ROOT_DOMAIN,
    })
    return templates.TemplateResponse("admin_accounts.html", ctx)


@app.post("/admin/accounts/approve")
async def admin_accounts_approve(request: Request,
                                 _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    try:
        account_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Missing account id."}, status_code=400)

    account = store.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found."}, status_code=404)
    if account["status"] != "approved" and store.count_approved_accounts() >= config.MAX_ACCOUNTS:
        return JSONResponse({"error": f"Already at the {config.MAX_ACCOUNTS}-account limit."},
                            status_code=403)

    trial_end_str = (body.get("trial_ends_at") or config.DEFAULT_TRIAL_END_DATE).strip()
    daily_minutes = int(body.get("daily_minutes_limit") or (config.DEFAULT_TRIAL_DAILY_SECONDS // 60))
    trial_ends_at = None
    if trial_end_str:
        with contextlib.suppress(ValueError):
            trial_ends_at = datetime.datetime.strptime(
                trial_end_str, "%Y-%m-%d"
            ).replace(hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc).timestamp()

    store.approve_account(account_id, trial_ends_at, daily_minutes)
    log.info("account approved: %s (%s)", account["business_name"], account["slug"])

    login_link = f"{request.url.scheme}://{request.url.netloc}/admin/login"
    with contextlib.suppress(Exception):
        await mailer.send_account_approved(account["email"], account["business_name"], login_link)

    return JSONResponse({"ok": True})


@app.post("/admin/accounts/update-plan")
async def admin_accounts_update_plan(request: Request,
                                     _: dict[str, Any] = Depends(_require_super)) -> Response:
    """The manual 'mark as paid' action - there is no PayPal/Razorpay
    integration yet, so this is how a sale (handled outside the app) actually
    takes effect on the account."""
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    try:
        account_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Missing account id."}, status_code=400)

    plan_type = (body.get("plan_type") or "").strip()
    if plan_type not in ("trial", "subscription", "onetime"):
        return JSONResponse({"error": "plan_type must be trial, subscription or onetime."},
                            status_code=400)

    daily_minutes = int(body.get("daily_minutes_limit") or 20)
    trial_end_str = (body.get("trial_ends_at") or "").strip()
    trial_ends_at = None
    if trial_end_str:
        with contextlib.suppress(ValueError):
            trial_ends_at = datetime.datetime.strptime(
                trial_end_str, "%Y-%m-%d"
            ).replace(hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc).timestamp()

    store.update_account_plan(account_id, plan_type, daily_minutes, trial_ends_at)
    log.info("account %s plan updated to %s (manual)", account_id, plan_type)
    return JSONResponse({"ok": True})


@app.post("/admin/accounts/reject")
async def admin_accounts_reject(request: Request,
                                _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    try:
        account_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Missing account id."}, status_code=400)
    store.reject_account(account_id)
    log.info("account rejected: %s", account_id)
    return JSONResponse({"ok": True})


@app.post("/admin/accounts/delete")
async def admin_accounts_delete(request: Request,
                                _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    try:
        account_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Missing account id."}, status_code=400)
    store.delete_account(account_id)
    log.info("account deleted: %s", account_id)
    return JSONResponse({"ok": True})


# --- Sub-account profile (business info + confirmed email change) --------

PROFILE_TEXT_FIELDS = {"business_name": 120, "website": 200, "phone": 40, "address": 300}


@app.post("/admin/profile")
async def update_profile(request: Request,
                         admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    if admin["account_id"] is None:
        return JSONResponse({"error": "Not available for the main account."}, status_code=400)
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    updates: dict[str, str] = {}
    for field, max_len in PROFILE_TEXT_FIELDS.items():
        value = body.get(field)
        if value is None or not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) > max_len:
            return JSONResponse(
                {"error": f"{field.replace('_', ' ')} is too long."}, status_code=400
            )
        updates[field] = value

    if "website" in updates:
        if not updates["website"]:
            return JSONResponse(
                {"error": "Website cannot be empty - callers are pointed there "
                          "whenever they hit a limit or the call ends."},
                status_code=400,
            )
        if not re.match(r"^https?://", updates["website"], re.IGNORECASE):
            updates["website"] = f"https://{updates['website']}"

    store.update_account_profile(admin["account_id"], **updates)
    log.info("account %s profile updated: %s", admin["account_id"], list(updates))
    return JSONResponse({"ok": True})


@app.post("/admin/profile/email-change")
async def request_email_change(request: Request,
                                admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    if admin["account_id"] is None:
        return JSONResponse({"error": "Not available for the main account."}, status_code=400)
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    new_email = (body.get("email") or "").strip().lower()[:200]
    if not new_email or "@" not in new_email:
        return JSONResponse({"error": "A valid email is required."}, status_code=400)
    if new_email == admin["email"]:
        return JSONResponse({"error": "That is already your email."}, status_code=400)

    # This email doubles as the login for whichever account owns it, so two
    # accounts sharing one would mean whoever confirms first silently takes
    # over the other's login.
    if new_email in config.ADMIN_EMAILS:
        return JSONResponse({"error": "That email is already in use."}, status_code=400)
    existing = store.get_account_by_email(new_email)
    if existing and existing["id"] != admin["account_id"] and existing["status"] == "approved":
        return JSONResponse({"error": "That email is already in use."}, status_code=400)

    try:
        security.check_cooldown(f"email-change:{admin['account_id']}", 30)
    except security.Denied as denied:
        return JSONResponse({"error": denied.message}, status_code=denied.status)

    token = secrets.token_urlsafe(32)
    store.start_email_change(admin["account_id"], new_email, token)
    link = f"{request.url.scheme}://{request.url.netloc}/admin/profile/confirm-email?token={token}"
    account = store.get_account(admin["account_id"])
    try:
        await mailer.send_email_change_confirmation(new_email, account["business_name"], link)
    except Exception as exc:
        log.error("failed to send email-change confirmation: %s", exc)
        return JSONResponse(
            {"error": "Could not send the confirmation email. Try again shortly."},
            status_code=502,
        )
    log.info("account %s requested email change to %s...", admin["account_id"], new_email[:3])
    return JSONResponse({"ok": True})


@app.get("/admin/profile/confirm-email")
async def admin_confirm_email_change(request: Request, token: str = "") -> Response:
    account = store.get_account_by_pending_token(token) if token else None
    new_email = store.confirm_email_change(account["id"]) if account else None
    if not new_email:
        return RedirectResponse("/admin/login?error=1")
    log.info("account %s email confirmed and changed to %s...", account["id"], new_email[:3])
    # The just-changed session cookie's baked-in email no longer matches this
    # account's row, so it is already invalid - clearing it explicitly avoids
    # leaving a dead cookie behind and sends them straight to a clean login.
    response = RedirectResponse("/admin/login")
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


# --- Support widget (bug reports / feedback) -----------------------------

@app.post("/admin/support/report")
async def submit_support_report(request: Request,
                                admin: dict[str, Any] = Depends(_require_admin)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    kind = (body.get("kind") or "").strip()
    if kind not in ("bug", "feedback"):
        return JSONResponse({"error": "Invalid report type."}, status_code=400)
    message = (body.get("message") or "").strip()[:2000]
    if not message:
        return JSONResponse({"error": "Message is required."}, status_code=400)

    contact_email = admin.get("email") or ""
    report_id = store.create_support_report(admin["account_id"], kind, message, contact_email)
    log.info("support report #%s (%s) from account=%s", report_id, kind, admin["account_id"])
    return JSONResponse({"ok": True})


@app.get("/admin/support", response_class=HTMLResponse)
async def admin_support_page(request: Request,
                             admin: dict[str, Any] = Depends(_require_super)) -> Response:
    ctx = _admin_base_context(request, "support", admin)
    reports_out = []
    for report in store.list_support_reports():
        report = dict(report)
        account = store.get_account(report["account_id"]) if report["account_id"] else None
        report["business_name"] = account["business_name"] if account else "Main account"
        report["when"] = datetime.datetime.fromtimestamp(
            report["created_at"], tz=datetime.timezone.utc
        ).strftime("%b %d, %H:%M")
        reports_out.append(report)
    ctx.update({
        "reports": reports_out,
        "calendar_link_value": store.get_setting(SETTINGS_KEY_CALENDAR_LINK),
    })
    return templates.TemplateResponse("admin_support.html", ctx)


@app.post("/admin/support/handled")
async def mark_support_handled(request: Request,
                               _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    try:
        report_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Missing report id."}, status_code=400)
    store.mark_report_handled(report_id)
    return JSONResponse({"ok": True})


@app.post("/admin/support/calendar-link")
async def save_calendar_link(request: Request,
                             _: dict[str, Any] = Depends(_require_super)) -> Response:
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    link = (body.get("calendar_link") or "").strip()[:300]
    store.set_setting(SETTINGS_KEY_CALENDAR_LINK, link)
    log.info("admin updated calendar link")
    return JSONResponse({"ok": True})


# --- Embeddable widget --------------------------------------------------

@app.get("/embed.js")
async def embed_script() -> Response:
    js = (config.BASE_DIR / "static" / "embed.js").read_text(encoding="utf-8")
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300"})
