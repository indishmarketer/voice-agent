"""External provider credentials and model choices - editable from
/admin/integrations without a code change or redeploy, same settings-table-
with-env-fallback pattern as branding.py and prompts.py's agent_rules.

Deliberately excludes SECRET_KEY and ADMIN_TOKEN, which stay Coolify-only:
those sign every session token this app issues, including the one that
would be needed to reach this very admin page, so changing them through a
UI they gate is circular, and rotating them invalidates every active
session (including the admin's own) at once. Same reasoning for the tuning
knobs (session limits, quotas, ALLOWED_ORIGINS) - those describe how we run
our own infrastructure, not a third-party service, so they stay in Coolify.

Everything below talks to a service outside our control - AssemblyAI,
Pollinations, Fish Audio, Cloudflare Turnstile. Keys rotate and model
lineups change; a text-box edit here should be enough to react to that.

resolve_for_account() is the one-time plan's whole mechanism: those
customers own their own usage cost (that is the entire point of the plan,
per pricing.md), so their calls resolve to ONLY their own stored keys -
deliberately no fallback to the owner's global ones, or the owner would
silently keep paying for their traffic. Every other case (the main account,
and trial/subscription sub-accounts, where the owner is paying either way)
resolves to the shared global configuration exactly as before this existed.

The two global daily caps (sessions, STT seconds) are the one exception to
"tuning knobs stay Coolify-only" - they are the platform-wide safety net
shared by every account at once (see security.enforce_quotas), and the owner
needs to be able to raise them himself mid-campaign without a redeploy, e.g.
to open capacity for a promotional push. Still env-fallback like everything
else here, just editable from /admin/integrations same as the rest.
"""
from typing import Optional

from . import config, store

SETTINGS_KEY_ASSEMBLYAI_API_KEY = "assemblyai_api_key"
SETTINGS_KEY_POLLINATIONS_API_KEY = "pollinations_api_key"
SETTINGS_KEY_FISH_API_KEY = "fish_api_key"
SETTINGS_KEY_FISH_MODEL_ID = "fish_model_id"
SETTINGS_KEY_TURNSTILE_SITE_KEY = "turnstile_site_key"
SETTINGS_KEY_TURNSTILE_SECRET_KEY = "turnstile_secret_key"
SETTINGS_KEY_POLLINATIONS_MODEL = "pollinations_model"
SETTINGS_KEY_FISH_MODEL = "fish_model"
SETTINGS_KEY_AAI_SPEECH_MODEL = "aai_speech_model"
SETTINGS_KEY_GLOBAL_SESSIONS_PER_DAY = "global_sessions_per_day"
SETTINGS_KEY_GLOBAL_STT_SECONDS_PER_DAY = "global_stt_seconds_per_day"


def assemblyai_api_key() -> str:
    return store.get_setting(SETTINGS_KEY_ASSEMBLYAI_API_KEY, config.ASSEMBLYAI_API_KEY)


def pollinations_api_key() -> str:
    return store.get_setting(SETTINGS_KEY_POLLINATIONS_API_KEY, config.POLLINATIONS_API_KEY)


def fish_api_key() -> str:
    return store.get_setting(SETTINGS_KEY_FISH_API_KEY, config.FISH_API_KEY)


def fish_model_id() -> str:
    return store.get_setting(SETTINGS_KEY_FISH_MODEL_ID, config.FISH_MODEL_ID)


def turnstile_site_key() -> str:
    return store.get_setting(SETTINGS_KEY_TURNSTILE_SITE_KEY, config.TURNSTILE_SITE_KEY)


def turnstile_secret_key() -> str:
    return store.get_setting(SETTINGS_KEY_TURNSTILE_SECRET_KEY, config.TURNSTILE_SECRET_KEY)


def pollinations_model() -> str:
    return store.get_setting(SETTINGS_KEY_POLLINATIONS_MODEL, config.POLLINATIONS_MODEL)


def fish_model() -> str:
    return store.get_setting(SETTINGS_KEY_FISH_MODEL, config.FISH_MODEL)


def aai_speech_model() -> str:
    return store.get_setting(SETTINGS_KEY_AAI_SPEECH_MODEL, config.AAI_SPEECH_MODEL)


def global_sessions_per_day() -> int:
    raw = store.get_setting(SETTINGS_KEY_GLOBAL_SESSIONS_PER_DAY, "")
    try:
        return int(raw) if raw else config.GLOBAL_SESSIONS_PER_DAY
    except ValueError:
        return config.GLOBAL_SESSIONS_PER_DAY


def global_stt_seconds_per_day() -> int:
    raw = store.get_setting(SETTINGS_KEY_GLOBAL_STT_SECONDS_PER_DAY, "")
    try:
        return int(raw) if raw else config.GLOBAL_STT_SECONDS_PER_DAY
    except ValueError:
        return config.GLOBAL_STT_SECONDS_PER_DAY


def missing_required() -> list[str]:
    """Names of provider credentials the app cannot run without, resolved
    the same way the app actually uses them (settings-table override, or
    env fallback) - not just the raw env vars, now that these can be set
    from /admin/integrations instead."""
    missing = []
    if not assemblyai_api_key():
        missing.append("ASSEMBLYAI_API_KEY")
    if not pollinations_api_key():
        missing.append("POLLINATIONS_API_KEY")
    if not fish_api_key():
        missing.append("FISH_API_KEY")
    return missing


def get_models() -> dict[str, str]:
    return {
        "pollinations_model": pollinations_model(),
        "fish_model": fish_model(),
        "aai_speech_model": aai_speech_model(),
    }


def resolve_for_account(account_id: Optional[int], plan_type: str = "trial") -> dict[str, str]:
    """The actual credentials/models one call should use, resolved once per
    session rather than read live from a client-supplied value - see the
    module docstring for why the one-time plan does not fall back to the
    global keys."""
    if account_id is not None and plan_type == "onetime":
        # fish_model_id (the voice reference) is included here deliberately,
        # unlike the curated preset system in voices.py - a Fish Audio voice
        # clone belongs to whichever account created it, so the owner's own
        # voice id would not work against a different account's API key.
        # An account on this plan has to bring its own voice, not pick from
        # the shared presets.
        return {
            "assemblyai_api_key": store.get_setting(SETTINGS_KEY_ASSEMBLYAI_API_KEY, "", account_id),
            "pollinations_api_key": store.get_setting(SETTINGS_KEY_POLLINATIONS_API_KEY, "", account_id),
            "fish_api_key": store.get_setting(SETTINGS_KEY_FISH_API_KEY, "", account_id),
            "fish_model_id": store.get_setting(SETTINGS_KEY_FISH_MODEL_ID, "", account_id),
            "pollinations_model": store.get_setting(
                SETTINGS_KEY_POLLINATIONS_MODEL, config.POLLINATIONS_MODEL, account_id
            ),
            "fish_model": store.get_setting(
                SETTINGS_KEY_FISH_MODEL, config.FISH_MODEL, account_id
            ),
            "aai_speech_model": store.get_setting(
                SETTINGS_KEY_AAI_SPEECH_MODEL, config.AAI_SPEECH_MODEL, account_id
            ),
        }
    return {
        "assemblyai_api_key": assemblyai_api_key(),
        "pollinations_api_key": pollinations_api_key(),
        "fish_api_key": fish_api_key(),
        "fish_model_id": fish_model_id(),
        "pollinations_model": pollinations_model(),
        "fish_model": fish_model(),
        "aai_speech_model": aai_speech_model(),
    }


def onetime_keys_configured(account_id: int) -> bool:
    """Whether a one-time-plan account has entered its own keys AND its own
    voice id yet - used to block its calls outright (with a clear message)
    rather than let resolve_for_account hand back empty strings that would
    just fail confusingly deep inside a provider call."""
    creds = resolve_for_account(account_id, "onetime")
    return bool(creds["assemblyai_api_key"] and creds["pollinations_api_key"]
               and creds["fish_api_key"] and creds["fish_model_id"])


def account_provider_keys(account_id: int) -> dict[str, str]:
    """Raw values for the account's own /admin/settings provider-keys form -
    unlike resolve_for_account, this does not care what plan_type is, since
    the form itself is what a sub-account uses to enter them in the first
    place."""
    return {
        "assemblyai_api_key": store.get_setting(SETTINGS_KEY_ASSEMBLYAI_API_KEY, "", account_id),
        "pollinations_api_key": store.get_setting(SETTINGS_KEY_POLLINATIONS_API_KEY, "", account_id),
        "fish_api_key": store.get_setting(SETTINGS_KEY_FISH_API_KEY, "", account_id),
        "fish_model_id": store.get_setting(SETTINGS_KEY_FISH_MODEL_ID, "", account_id),
    }


def get_integrations() -> dict[str, str]:
    """Real values - used both by the app itself and to populate the
    settings form. The route that renders the form is responsible for
    masking the secret fields before they ever reach the template, same as
    a password field never round-trips its old value."""
    return {
        "assemblyai_api_key": assemblyai_api_key(),
        "pollinations_api_key": pollinations_api_key(),
        "fish_api_key": fish_api_key(),
        "fish_model_id": fish_model_id(),
        "turnstile_site_key": turnstile_site_key(),
        "turnstile_secret_key": turnstile_secret_key(),
        **get_models(),
    }
