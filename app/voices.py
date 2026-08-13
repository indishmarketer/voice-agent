"""Per-account voice selection.

A small fixed list of Fish Audio voices the owner has pre-approved, rather
than an open text field - a sub-account picks from four presets instead of
pasting an arbitrary Fish Audio reference id. The main account is untouched:
it always keeps using whatever voice is configured on /admin/integrations,
exactly as before this existed.
"""
from typing import Optional

from . import store

SETTINGS_KEY_VOICE_PRESET = "voice_preset"

# fish_model_id: None means "use the owner's own configured voice" (see
# integrations.fish_model_id()) - that is Indian English (Male) today, and
# stays correct automatically if the owner ever changes that voice, rather
# than freezing today's id here too.
VOICE_PRESETS: dict[str, dict[str, Optional[str]]] = {
    "english_male": {
        "label": "English (Male)",
        "fish_model_id": "536d3a5e000945adb7038665781a4aca",
    },
    "english_female": {
        "label": "English (Female)",
        "fish_model_id": "9a9cf47702da476aa4629e2506d4a857",
    },
    "indian_male": {
        "label": "Indian English (Male)",
        "fish_model_id": None,
    },
    "indian_female": {
        "label": "Indian English (Female)",
        "fish_model_id": "7d62e110d5364b608912d6837890b336",
    },
}
# Display/dropdown order, per the owner's spec - English first, then Indian.
VOICE_PRESET_ORDER = ["english_male", "english_female", "indian_male", "indian_female"]

DEFAULT_PRESET_INDIA = "indian_male"
DEFAULT_PRESET_OTHER = "english_female"


def default_preset_for_country(country: str) -> str:
    return DEFAULT_PRESET_INDIA if (country or "").strip().lower() == "india" \
        else DEFAULT_PRESET_OTHER


def account_voice_preset(account_id: Optional[int]) -> str:
    """None (the main account) is not on this system at all - callers should
    check for that before calling this."""
    account = store.get_account(account_id) if account_id is not None else None
    default = default_preset_for_country(account["country"] if account else "")
    preset = store.get_setting(SETTINGS_KEY_VOICE_PRESET, default, account_id)
    return preset if preset in VOICE_PRESETS else default


def account_fish_model_id(account_id: Optional[int]) -> Optional[str]:
    """The Fish Audio reference_id for this account's calls. None means "use
    the owner's own globally-configured voice" - both for the main account,
    and for any sub-account currently on the Indian English (Male) preset."""
    if account_id is None:
        return None
    return VOICE_PRESETS[account_voice_preset(account_id)]["fish_model_id"]


def all_voice_ids(default_voice_id: str) -> list[str]:
    """Every distinct Fish Audio id currently reachable by any account -
    used to pre-render filler clips for all of them, not just the default."""
    ids = {default_voice_id}
    for preset in VOICE_PRESETS.values():
        if preset["fish_model_id"]:
            ids.add(preset["fish_model_id"])
    return list(ids)
