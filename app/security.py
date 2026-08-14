"""Abuse protection: bot checks, signed session tokens, quotas, origin checks.

The threat model is "public page with no login". We cannot identify people, so
we stack cheap independent limits instead:

  1. Cloudflare Turnstile   - keeps scripted abuse out entirely (free).
  2. Signed session token   - the websocket refuses anything we did not issue.
  3. AssemblyAI temp token  - one-time use, and AssemblyAI itself enforces the
                              maximum session length, so a hijacked token still
                              cannot burn more than SESSION_MAX_SECONDS.
  4. Per-IP / per-visitor / global daily caps and a concurrency cap.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Optional

import httpx
from fastapi import Request

from . import branding, config, integrations, store


class Denied(Exception):
    """Raised when a request should not be allowed to start a session."""

    def __init__(self, reason: str, message: str, status: int = 429):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


# --- Signed tokens ----------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def sign(payload: dict[str, Any], ttl_seconds: int) -> str:
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(config.SECRET_KEY.encode(), raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(mac)}"


def verify(token: str) -> Optional[dict[str, Any]]:
    try:
        body_b64, mac_b64 = token.split(".", 1)
        raw = _b64d(body_b64)
        expected = hmac.new(config.SECRET_KEY.encode(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(mac_b64)):
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


def is_admin(token: str) -> bool:
    if not config.ADMIN_TOKEN:
        return False
    return hmac.compare_digest(token or "", config.ADMIN_TOKEN)


# --- Request identity -------------------------------------------------------

def client_ip(request: Request) -> str:
    """Left-most X-Forwarded-For hop when we sit behind Coolify's Traefik."""
    if config.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def hash_ip(ip: str) -> str:
    """Store a keyed hash rather than the raw address."""
    return hmac.new(
        config.SECRET_KEY.encode(), ip.encode(), hashlib.sha256
    ).hexdigest()[:32]


def origin_allowed(origin: str) -> bool:
    """Empty ALLOWED_ORIGINS means 'not configured yet' - allow, but warn in logs."""
    if not config.ALLOWED_ORIGINS:
        return True
    return (origin or "").rstrip("/") in config.ALLOWED_ORIGINS


# --- Bot check --------------------------------------------------------------

async def verify_turnstile(token: str, ip: str) -> bool:
    secret = integrations.turnstile_secret_key()
    if not secret:
        return True  # not configured - skip rather than lock everyone out
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": secret,
                    "response": token,
                    "remoteip": ip,
                },
            )
        return bool(res.json().get("success"))
    except Exception:
        # Fail closed: if we cannot verify, do not hand out AssemblyAI minutes.
        return False


# --- Concurrency ------------------------------------------------------------

_active: dict[str, float] = {}


def _prune() -> None:
    cutoff = time.time() - (config.SESSION_MAX_SECONDS + 60)
    for sid in [s for s, started in _active.items() if started < cutoff]:
        _active.pop(sid, None)


def acquire_slot(session_id: str) -> None:
    _prune()
    if len(_active) >= config.MAX_CONCURRENT_SESSIONS:
        raise Denied(
            "busy",
            "All lines are busy right now. Please try again in a minute.",
            503,
        )
    _active[session_id] = time.time()


def release_slot(session_id: str) -> None:
    _active.pop(session_id, None)


def active_count() -> int:
    _prune()
    return len(_active)


# --- Simple cooldowns ---------------------------------------------------
#
# Not a general rate limiter - just "one of these per key per N seconds",
# for the handful of unauthenticated POST endpoints (magic-link requests,
# beta applications) that have no other throttle. In-process and best
# effort, same trade-off as _active above: fine for a single-worker
# deployment, would need Redis to scale beyond one.

_cooldowns: dict[str, float] = {}


def check_cooldown(key: str, seconds: float) -> None:
    """Raises Denied if this key was last used within the cooldown window,
    otherwise records this use and lets it through."""
    now = time.time()
    last = _cooldowns.get(key)
    if last and now - last < seconds:
        raise Denied(
            "cooldown",
            "Please wait a moment before trying again.",
            429,
        )
    _cooldowns[key] = now
    # Opportunistic cleanup so this dict cannot grow forever - cheap relative
    # to how rarely these endpoints are hit.
    if len(_cooldowns) > 5000:
        cutoff = now - 3600
        for k in [k for k, t in _cooldowns.items() if t < cutoff]:
            _cooldowns.pop(k, None)


# --- Accounts (sub-account trial/plan gating) -------------------------------

def check_account(account: Optional[dict[str, Any]]) -> None:
    """Raises Denied if this account cannot take a call right now. Called
    before enforce_quotas so an expired trial reads as "please subscribe",
    not a generic rate-limit message.

    account is None for the original single-tenant agent (no gate at all -
    it predates sub-accounts and keeps its existing quota-only behaviour).
    """
    if account is None:
        return
    if account["status"] != "approved":
        raise Denied(
            "account_not_active",
            "This voice agent is not active yet. Please contact its owner.",
            403,
        )
    if account["plan_type"] == "trial" and account["trial_ends_at"] and \
            time.time() >= account["trial_ends_at"]:
        raise Denied(
            "trial_expired",
            "This free trial has ended. Please subscribe to keep using this "
            "voice agent.",
            402,
        )


def check_account_daily_cap(account: Optional[dict[str, Any]]) -> None:
    """Only the free trial has a DAILY cap. 'subscription' has its own
    MONTHLY cap instead (see check_account_monthly_cap below); 'onetime' is
    unmetered here since the customer's own provider keys pay for their own
    usage, not the owner's."""
    if account is None or account["plan_type"] != "trial":
        return
    limit_seconds = int(account["daily_minutes_limit"]) * 60
    used = store.stt_seconds_today(account_id=account["id"])
    if used >= limit_seconds:
        raise Denied(
            "account_daily_cap",
            f"Today's {account['daily_minutes_limit']}-minute free limit has "
            "been used up. Please try again tomorrow, or subscribe for "
            "unlimited access.",
            429,
        )


def check_account_monthly_cap(account: Optional[dict[str, Any]]) -> None:
    """The subscription plan's fair-use ceiling - the owner pays for every
    minute on this plan, unlike 'onetime' where the customer's own keys pay
    for their own usage, so this is the actual cost-control backstop for it."""
    if account is None or account["plan_type"] != "subscription":
        return
    limit_seconds = config.SUBSCRIPTION_MONTHLY_MINUTES_CAP * 60
    used = store.stt_seconds_this_month(account["id"])
    if used >= limit_seconds:
        raise Denied(
            "account_monthly_cap",
            f"This month's {config.SUBSCRIPTION_MONTHLY_MINUTES_CAP}-minute "
            "fair-use limit has been reached. It resets next month - "
            "contact us if you need more.",
            429,
        )


# --- Quotas -----------------------------------------------------------------

def enforce_quotas(ip_hash: str, visitor_id: str, account_id: Optional[int] = None) -> None:
    """Raises Denied when any budget is exhausted. Cheapest checks first.

    global_daily/global_minutes are this deployment's own infra-wide safety
    net (protecting the shared API budget across every account at once), so
    those always point at the platform owner's site regardless of whose
    call tripped them. ip_daily/visitor_daily happen inside one specific
    account's call, so those point at THAT account's own site - otherwise a
    sub-account's callers were being told to go to the platform owner's
    site instead of the business they were actually calling.
    """
    if store.count_sessions_today() >= config.GLOBAL_SESSIONS_PER_DAY:
        raise Denied(
            "global_daily",
            "The demo has reached its limit for today. Please try again tomorrow "
            f"or reach us at {config.WEBSITE_URL}.",
        )
    if store.stt_seconds_today() >= config.GLOBAL_STT_SECONDS_PER_DAY:
        raise Denied(
            "global_minutes",
            "The demo has reached its limit for today. Please try again tomorrow "
            f"or reach us at {config.WEBSITE_URL}.",
        )
    site = branding.website_url(account_id)
    if store.count_sessions_today(ip_hash=ip_hash) >= config.SESSIONS_PER_IP_PER_DAY:
        raise Denied(
            "ip_daily",
            "You have used up today's free calls. Please come back tomorrow, or "
            f"book a real conversation at {site}.",
        )
    if store.count_sessions_today(visitor_id=visitor_id) >= config.SESSIONS_PER_VISITOR_PER_DAY:
        raise Denied(
            "visitor_daily",
            "You have used up today's free calls. Please come back tomorrow, or "
            f"book a real conversation at {site}.",
        )
