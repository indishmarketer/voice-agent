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
"""
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
