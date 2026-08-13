"""Public-facing branding: logo, brand name, tagline, agent name, greeting.

Same pattern as prompts.py's agent_rules - a settings-table override with the
env var (or a plain default) as the fallback, so a fresh deploy with nothing
configured in /admin/settings still looks right. This only covers the call
app's own branding; the admin dashboard's own "Voice Agent" identity is
intentionally left hardcoded, see templates/admin_base.html.
"""
import io
from typing import Optional

from . import config, store

SETTINGS_KEY_BRAND_NAME = "brand_name"
SETTINGS_KEY_TAGLINE = "tagline"
SETTINGS_KEY_AGENT_NAME = "agent_name"
SETTINGS_KEY_GREETING = "greeting"

DEFAULT_TAGLINE = "Intelligent Voice Assistant"

LOGO_PATH = config.BASE_DIR / "static" / "img" / "logo.png"
FAVICON_PATH = config.BASE_DIR / "static" / "img" / "favicon.png"
APPLE_TOUCH_ICON_PATH = config.BASE_DIR / "static" / "img" / "apple-touch-icon.png"

_FILENAMES = {"logo": "logo.png", "favicon": "favicon.png",
             "apple_touch_icon": "apple-touch-icon.png"}


def _account_branding_dir(account_id: int):
    return config.DATA_DIR / "accounts" / str(account_id) / "branding"


def logo_paths(account_id: Optional[int] = None) -> dict[str, "object"]:
    """Every sub-account gets its own logo/favicon/apple-touch-icon on disk -
    previously these were one shared file, so a sub-account uploading a logo
    would have silently replaced the main account's. None is the original
    single-tenant agent, unchanged."""
    if account_id is None:
        return {"logo": LOGO_PATH, "favicon": FAVICON_PATH,
                "apple_touch_icon": APPLE_TOUCH_ICON_PATH}
    d = _account_branding_dir(account_id)
    return {kind: d / name for kind, name in _FILENAMES.items()}


def logo_url(account_id: Optional[int] = None, kind: str = "logo") -> str:
    """URL for /branding/<scope>/<file>, served by the route in main.py.
    Falls back to the shared default asset if this account has not uploaded
    its own - so a fresh sub-account looks right immediately, same as its
    text branding defaults to its business name until customised."""
    path = logo_paths(account_id)[kind]
    if account_id is not None and not path.exists():
        path = logo_paths(None)[kind]
        scope = "default"
    else:
        scope = "default" if account_id is None else str(account_id)
    try:
        version = int(path.stat().st_mtime) if path.exists() else 0
    except OSError:
        version = 0
    return f"/branding/{scope}/{_FILENAMES[kind]}?v={version}"


def get_branding(account_id: Optional[int] = None) -> dict[str, str]:
    """The current effective branding - saved overrides, or seed defaults.

    A sub-account with nothing customised yet must NOT default to the main
    account's brand/agent name - that was the "why does my agent introduce
    itself as Indish Marketer" bug. Its seed defaults are derived from its
    own business_name instead, so a fresh account already sounds like it
    belongs to that business before anyone edits a single setting.
    """
    if account_id is None:
        default_brand = config.BRAND_NAME
        default_agent = config.AGENT_NAME
        default_greeting = config.GREETING
    else:
        account = store.get_account(account_id)
        business = (account["business_name"] if account else "") or "your business"
        default_brand = business
        default_agent = f"{business} Assistant"
        default_greeting = f"Hi, welcome to {business}. How can I help you today?"
    return {
        "brand_name": store.get_setting(SETTINGS_KEY_BRAND_NAME, default_brand, account_id),
        "tagline": store.get_setting(SETTINGS_KEY_TAGLINE, DEFAULT_TAGLINE, account_id),
        "agent_name": store.get_setting(SETTINGS_KEY_AGENT_NAME, default_agent, account_id),
        "greeting": store.get_setting(SETTINGS_KEY_GREETING, default_greeting, account_id),
    }


def _round_corners(image, radius_fraction: float):
    """Bake rounded corners into the pixels themselves.

    In-page logo placements can just use CSS border-radius, but a favicon is
    rendered by the browser from the raw image bytes - there is no CSS to
    apply, so the rounding has to already be part of the file.
    """
    from PIL import Image, ImageDraw

    size = image.size[0]
    radius = int(size * radius_fraction)
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), image.size], radius=radius, fill=255)

    rounded = Image.new("RGBA", image.size, (0, 0, 0, 0))
    rounded.paste(image.convert("RGBA"), (0, 0), mask)
    return rounded


def regenerate_logo_assets(image_bytes: bytes, account_id: Optional[int] = None) -> None:
    """Replace logo.png with the upload, and regenerate favicon +
    apple-touch-icon from it so all three stay in sync. Writes to this
    account's own files (see logo_paths) - never the shared default unless
    account_id is None."""
    from PIL import Image

    paths = logo_paths(account_id)
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    source = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    # Square it against the longer side so an oblong upload does not distort.
    side = max(source.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(source, ((side - source.size[0]) // 2, (side - source.size[1]) // 2))

    logo = square.resize((320, 320), Image.LANCZOS)
    logo.save(paths["logo"])

    favicon = _round_corners(square.resize((64, 64), Image.LANCZOS), 0.22)
    favicon.save(paths["favicon"])

    apple_icon = _round_corners(square.resize((180, 180), Image.LANCZOS), 0.22)
    apple_icon.save(paths["apple_touch_icon"])
