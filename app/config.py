"""Central configuration. Every secret comes from the environment - nothing is hardcoded."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --- Provider credentials (set these in Coolify, never in code) -------------
ASSEMBLYAI_API_KEY = _env("ASSEMBLYAI_API_KEY")
POLLINATIONS_API_KEY = _env("POLLINATIONS_API_KEY")
FISH_API_KEY = _env("FISH_API_KEY")

# Measured on this workload: comparable time-to-first-token to the other free
# models, but noticeably shorter replies, which matters on a voice call.
POLLINATIONS_MODEL = _env("POLLINATIONS_MODEL", "gemini-fast")
POLLINATIONS_BASE = _env("POLLINATIONS_BASE", "https://gen.pollinations.ai/v1")

FISH_MODEL_ID = _env("FISH_MODEL_ID", "e0ea460db14b4430afb6311708937b6d")
FISH_MODEL = _env("FISH_MODEL", "s2.1-pro-free")
FISH_WS_URL = "wss://api.fish.audio/v1/tts/live"
# 24 kHz mono PCM: good voice quality at half the bandwidth of 44.1 kHz.
FISH_SAMPLE_RATE = _int("FISH_SAMPLE_RATE", 24000)
FISH_LATENCY = _env("FISH_LATENCY", "balanced")  # "normal" | "balanced"

AAI_TOKEN_URL = "https://streaming.assemblyai.com/v3/token"
AAI_WS_BASE = "wss://streaming.assemblyai.com/v3/ws"
AAI_SPEECH_MODEL = _env("AAI_SPEECH_MODEL", "universal-3-5-pro")
AAI_SAMPLE_RATE = 16000

# --- App secrets ------------------------------------------------------------
# SECRET_KEY signs session tokens and the visitor cookie. If it is not set we
# generate an ephemeral one, which means sessions reset on every restart.
SECRET_KEY = _env("SECRET_KEY") or secrets.token_hex(32)
ADMIN_TOKEN = _env("ADMIN_TOKEN")

# --- Abuse protection -------------------------------------------------------
TURNSTILE_SITE_KEY = _env("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET_KEY = _env("TURNSTILE_SECRET_KEY")

# Hard ceiling AssemblyAI itself enforces on every minted token.
SESSION_MAX_SECONDS = _int("SESSION_MAX_SECONDS", 420)
# Server-side inactivity hangup.
SESSION_IDLE_SECONDS = _int("SESSION_IDLE_SECONDS", 45)
MAX_TURNS_PER_SESSION = _int("MAX_TURNS_PER_SESSION", 25)
MAX_USER_CHARS_PER_TURN = _int("MAX_USER_CHARS_PER_TURN", 600)

SESSIONS_PER_IP_PER_DAY = _int("SESSIONS_PER_IP_PER_DAY", 3)
SESSIONS_PER_VISITOR_PER_DAY = _int("SESSIONS_PER_VISITOR_PER_DAY", 4)
GLOBAL_SESSIONS_PER_DAY = _int("GLOBAL_SESSIONS_PER_DAY", 120)
# Belt and braces: stop minting once measured STT seconds hit the daily budget.
GLOBAL_STT_SECONDS_PER_DAY = _int("GLOBAL_STT_SECONDS_PER_DAY", 3600)
MAX_CONCURRENT_SESSIONS = _int("MAX_CONCURRENT_SESSIONS", 8)

ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in _env("ALLOWED_ORIGINS").split(",")
    if o.strip()
]
TRUST_PROXY = _bool("TRUST_PROXY", True)

# --- Behaviour --------------------------------------------------------------
AGENT_NAME = _env("AGENT_NAME", "Indish Marketer Assistant")
BRAND_NAME = _env("BRAND_NAME", "Indish Marketer")
WEBSITE_URL = _env("WEBSITE_URL", "https://indishmarketer.com")
GREETING = _env("GREETING", "Hi, welcome to Indish Marketer. How can I help you today?")

KNOWLEDGE_DIR = Path(_env("KNOWLEDGE_DIR", str(BASE_DIR / "knowledge")))
DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "agent.db"

# If the whole knowledge base fits under this many characters we inline all of
# it; above that we fall back to BM25 retrieval of the most relevant chunks.
KB_INLINE_LIMIT = _int("KB_INLINE_LIMIT", 7000)
KB_TOP_K = _int("KB_TOP_K", 4)

# Short pre-rendered "sure"/"got it" clips that cover the model's think time.
ENABLE_FILLERS = _bool("ENABLE_FILLERS", True)
# Seconds to wait after the acknowledgement before adding a second filler.
FILLER_BRIDGE_AFTER = float(_env("FILLER_BRIDGE_AFTER", "0.9"))

LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 120)
# Hard stop on how much the agent may say in one turn, whatever the model does.
MAX_REPLY_CHARS = _int("MAX_REPLY_CHARS", 400)
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.6"))
HISTORY_TURNS = _int("HISTORY_TURNS", 12)

SHEETS_WEBHOOK_URL = _env("SHEETS_WEBHOOK_URL")
SHEETS_WEBHOOK_SECRET = _env("SHEETS_WEBHOOK_SECRET")
# The human-viewable spreadsheet, for the "open spreadsheet" button in admin.
# Distinct from SHEETS_WEBHOOK_URL, which is the Apps Script endpoint that
# receives lead data and isn't meant to be opened in a browser.
SHEETS_URL = _env(
    "SHEETS_URL",
    "https://docs.google.com/spreadsheets/d/"
    "1Pk0faDONiMpL5EgbUudXPjZ770IkW3g1WoUrcKM6N78/edit?gid=0#gid=0",
)


def missing_required() -> list[str]:
    """Names of credentials the app cannot run without."""
    missing = []
    for name in ("ASSEMBLYAI_API_KEY", "POLLINATIONS_API_KEY", "FISH_API_KEY"):
        if not globals()[name]:
            missing.append(name)
    return missing
