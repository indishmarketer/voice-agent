"""Which model each AI provider call uses - editable from /admin/settings
without a code change or redeploy, same settings-table-with-env-fallback
pattern as branding.py and prompts.py's agent_rules. Provider prices and
model lineups change; a text-box edit here should be enough to react to
that, not a trip through the environment variables and a redeploy.
"""
from . import config, store

SETTINGS_KEY_POLLINATIONS_MODEL = "pollinations_model"
SETTINGS_KEY_FISH_MODEL = "fish_model"
SETTINGS_KEY_AAI_SPEECH_MODEL = "aai_speech_model"


def pollinations_model() -> str:
    return store.get_setting(SETTINGS_KEY_POLLINATIONS_MODEL, config.POLLINATIONS_MODEL)


def fish_model() -> str:
    return store.get_setting(SETTINGS_KEY_FISH_MODEL, config.FISH_MODEL)


def aai_speech_model() -> str:
    return store.get_setting(SETTINGS_KEY_AAI_SPEECH_MODEL, config.AAI_SPEECH_MODEL)


def get_models() -> dict[str, str]:
    return {
        "pollinations_model": pollinations_model(),
        "fish_model": fish_model(),
        "aai_speech_model": aai_speech_model(),
    }
