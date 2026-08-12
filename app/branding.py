"""Public-facing branding: logo, brand name, tagline, agent name, greeting.

Same pattern as prompts.py's agent_rules - a settings-table override with the
env var (or a plain default) as the fallback, so a fresh deploy with nothing
configured in /admin/settings still looks right. This only covers the call
app's own branding; the admin dashboard's own "Voice Agent" identity is
intentionally left hardcoded, see templates/admin_base.html.
"""
import io

from . import config, store

SETTINGS_KEY_BRAND_NAME = "brand_name"
SETTINGS_KEY_TAGLINE = "tagline"
SETTINGS_KEY_AGENT_NAME = "agent_name"
SETTINGS_KEY_GREETING = "greeting"

DEFAULT_TAGLINE = "Intelligent Voice Assistant"

LOGO_PATH = config.BASE_DIR / "static" / "img" / "logo.png"
FAVICON_PATH = config.BASE_DIR / "static" / "img" / "favicon.png"
APPLE_TOUCH_ICON_PATH = config.BASE_DIR / "static" / "img" / "apple-touch-icon.png"


def get_branding() -> dict[str, str]:
    """The current effective branding - saved overrides, or seed defaults."""
    return {
        "brand_name": store.get_setting(SETTINGS_KEY_BRAND_NAME, config.BRAND_NAME),
        "tagline": store.get_setting(SETTINGS_KEY_TAGLINE, DEFAULT_TAGLINE),
        "agent_name": store.get_setting(SETTINGS_KEY_AGENT_NAME, config.AGENT_NAME),
        "greeting": store.get_setting(SETTINGS_KEY_GREETING, config.GREETING),
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


def regenerate_logo_assets(image_bytes: bytes) -> None:
    """Replace logo.png with the upload, and regenerate favicon +
    apple-touch-icon from it so all three stay in sync."""
    from PIL import Image

    source = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    # Square it against the longer side so an oblong upload does not distort.
    side = max(source.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(source, ((side - source.size[0]) // 2, (side - source.size[1]) // 2))

    logo = square.resize((320, 320), Image.LANCZOS)
    logo.save(LOGO_PATH)

    favicon = _round_corners(square.resize((64, 64), Image.LANCZOS), 0.22)
    favicon.save(FAVICON_PATH)

    apple_icon = _round_corners(square.resize((180, 180), Image.LANCZOS), 0.22)
    apple_icon.save(APPLE_TOUCH_ICON_PATH)
