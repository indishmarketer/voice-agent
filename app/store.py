"""SQLite persistence: visitor memory, transcripts, leads and usage counters.

Everything lives in one file on a Docker volume so it survives redeploys.
"""
import sqlite3
import time
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id           TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    name         TEXT,
    email        TEXT,
    phone        TEXT,
    summary      TEXT,
    session_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    visitor_id   TEXT NOT NULL,
    ip_hash      TEXT,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    stt_seconds  REAL NOT NULL DEFAULT 0,
    turn_count   INTEGER NOT NULL DEFAULT 0,
    day          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);
CREATE INDEX IF NOT EXISTS idx_sessions_ip ON sessions(ip_hash, day);
CREATE INDEX IF NOT EXISTS idx_sessions_visitor ON sessions(visitor_id, day);

CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    name         TEXT,
    email        TEXT,
    phone        TEXT,
    company      TEXT,
    problem      TEXT,
    interest     TEXT,
    qualified    INTEGER NOT NULL DEFAULT 0,
    transcript   TEXT,
    synced       INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);

-- Free-form admin-editable config (agent behaviour rules, greeting, etc.) so
-- changing how the agent behaves does not require a code edit and redeploy.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Magic-link admin login. One row per link sent; used/expired ones are
-- rejected on verification so a link only ever works once.
CREATE TABLE IF NOT EXISTS login_tokens (
    token      TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- Sub-account applications and (once approved) live trial accounts. A
-- business applies, the owner approves/rejects from admin, and only on
-- approval does the slug become a live /a/<slug> page. Rejected/pending rows
-- are kept (not deleted) so a resubmit with the same email is visible.
CREATE TABLE IF NOT EXISTS accounts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                 TEXT UNIQUE NOT NULL,
    business_name        TEXT NOT NULL,
    website              TEXT,
    email                TEXT NOT NULL,
    note                 TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    plan_type            TEXT NOT NULL DEFAULT 'trial',
    daily_minutes_limit  INTEGER NOT NULL DEFAULT 20,
    trial_ends_at        REAL,
    last_limit_email_at  REAL,
    created_at           REAL NOT NULL,
    approved_at          REAL
);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def init() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(SCHEMA)
        # WAL keeps reads from blocking the websocket handlers.
        _conn.execute("PRAGMA journal_mode=WAL")
        # sessions/leads/turns predate sub-accounts - add the column for
        # existing DBs rather than requiring a fresh volume. NULL account_id
        # means "the main/default agent", so nothing about the existing live
        # deployment changes behaviour.
        for table in ("sessions", "leads", "turns"):
            cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
            if "account_id" not in cols:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER")
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_account_day "
            "ON sessions(account_id, day)"
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_account ON leads(account_id)"
        )
        _conn.commit()


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    assert _conn is not None, "store.init() was never called"
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    assert _conn is not None, "store.init() was never called"
    with _lock:
        return _conn.execute(sql, params).fetchall()


# --- Visitors ---------------------------------------------------------------

def visitor_key(raw_visitor_id: str, account_id: Optional[int]) -> str:
    """Namespaces the browser cookie's visitor id by account.

    The `visitors` table's primary key predates sub-accounts and is just the
    raw cookie value, with no account column to add one to (it *is* the
    primary key). Without this, the same browser visiting two different
    businesses' embedded widgets - both served from this one domain - would
    share one memory row, leaking one company's caller history/greeting into
    the other's. account_id None (the original single-tenant agent) is left
    unprefixed so existing rows for the live deployment keep resolving
    exactly as they did before sub-accounts existed.
    """
    return raw_visitor_id if account_id is None else f"a{account_id}:{raw_visitor_id}"


def touch_visitor(visitor_id: str) -> dict[str, Any]:
    now = time.time()
    row = _query("SELECT * FROM visitors WHERE id = ?", (visitor_id,))
    if not row:
        _exec(
            "INSERT INTO visitors (id, created_at, last_seen) VALUES (?, ?, ?)",
            (visitor_id, now, now),
        )
        return {"id": visitor_id, "summary": None, "name": None,
                "email": None, "phone": None, "session_count": 0}
    _exec("UPDATE visitors SET last_seen = ? WHERE id = ?", (now, visitor_id))
    return dict(row[0])


def update_visitor_memory(visitor_id: str, summary: Optional[str] = None,
                          name: Optional[str] = None, email: Optional[str] = None,
                          phone: Optional[str] = None) -> None:
    sets, params = [], []
    for col, val in (("summary", summary), ("name", name),
                     ("email", email), ("phone", phone)):
        if val:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    params.append(visitor_id)
    _exec(f"UPDATE visitors SET {', '.join(sets)} WHERE id = ?", tuple(params))


# --- Sessions ---------------------------------------------------------------

def create_session(session_id: str, visitor_id: str, ip_hash: str,
                   account_id: Optional[int] = None) -> None:
    _exec(
        "INSERT INTO sessions (id, visitor_id, ip_hash, started_at, day, account_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, visitor_id, ip_hash, time.time(), _today(), account_id),
    )
    _exec(
        "UPDATE visitors SET session_count = session_count + 1 WHERE id = ?",
        (visitor_id,),
    )


def end_session(session_id: str, stt_seconds: float, turn_count: int) -> None:
    _exec(
        "UPDATE sessions SET ended_at = ?, stt_seconds = ?, turn_count = ? WHERE id = ?",
        (time.time(), stt_seconds, turn_count, session_id),
    )


def count_sessions_today(ip_hash: str = "", visitor_id: str = "",
                         account_id: Optional[int] = None) -> int:
    """account_id, when given, always narrows the count further - combine
    with ip_hash/visitor_id, or pass alone for a per-account daily count."""
    day = _today()
    clauses, params = ["day = ?"], [day]
    if ip_hash:
        clauses.append("ip_hash = ?")
        params.append(ip_hash)
    if visitor_id:
        clauses.append("visitor_id = ?")
        params.append(visitor_id)
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(account_id)
    sql = f"SELECT COUNT(*) c FROM sessions WHERE {' AND '.join(clauses)}"
    return _query(sql, tuple(params))[0]["c"]


def stt_seconds_today(account_id: Optional[int] = None) -> float:
    if account_id is not None:
        row = _query(
            "SELECT COALESCE(SUM(stt_seconds), 0) s FROM sessions "
            "WHERE day = ? AND account_id = ?",
            (_today(), account_id),
        )
    else:
        row = _query(
            "SELECT COALESCE(SUM(stt_seconds), 0) s FROM sessions WHERE day = ?",
            (_today(),),
        )
    return float(row[0]["s"])


# --- Transcript -------------------------------------------------------------

def add_turn(session_id: str, visitor_id: str, role: str, content: str,
            account_id: Optional[int] = None) -> None:
    _exec(
        "INSERT INTO turns (session_id, visitor_id, role, content, created_at, account_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, visitor_id, role, content, time.time(), account_id),
    )


def recent_turns(visitor_id: str, limit: int = 40) -> list[dict[str, Any]]:
    rows = _query(
        "SELECT role, content, created_at FROM turns WHERE visitor_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (visitor_id, limit),
    )
    return [dict(r) for r in reversed(rows)]


# --- Leads ------------------------------------------------------------------

def save_lead(session_id: str, visitor_id: str, data: dict[str, Any],
              transcript: str, account_id: Optional[int] = None) -> int:
    """Upsert by session_id.

    A lead can be written twice for the same call: once immediately when the
    caller confirms the on-screen contact card mid-call, and again from the
    post-call pass that fills in company/problem/interest from the full
    transcript. Without this being an upsert, that would create two rows per
    confirmed lead. A field is only overwritten when the new value is
    non-empty, so the second write fills in gaps rather than blanking out
    what the first one already captured.
    """
    existing = _query("SELECT * FROM leads WHERE session_id = ?", (session_id,))
    if existing:
        row = dict(existing[0])
        merged = {
            key: (data.get(key) or row.get(key))
            for key in ("name", "email", "phone", "company", "problem", "interest")
        }
        _exec(
            "UPDATE leads SET name=?, email=?, phone=?, company=?, problem=?, "
            "interest=?, qualified=?, transcript=? WHERE id=?",
            (
                merged["name"], merged["email"], merged["phone"],
                merged["company"], merged["problem"], merged["interest"],
                1 if (merged["email"] or merged["phone"]) else 0,
                transcript or row.get("transcript"),
                row["id"],
            ),
        )
        return int(row["id"])

    cur = _exec(
        "INSERT INTO leads (session_id, visitor_id, name, email, phone, company, "
        "problem, interest, qualified, transcript, created_at, account_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, visitor_id,
            data.get("name"), data.get("email"), data.get("phone"),
            data.get("company"), data.get("problem"), data.get("interest"),
            1 if (data.get("email") or data.get("phone")) else 0,
            transcript, time.time(), account_id,
        ),
    )
    return int(cur.lastrowid or 0)


def mark_lead_synced(lead_id: int) -> None:
    _exec("UPDATE leads SET synced = 1 WHERE id = ?", (lead_id,))


def _account_clause(account_id: Optional[int]) -> tuple[str, tuple]:
    """account_id is None for the original single-tenant agent, whose rows
    were written before the column existed - those are NULL, not 0/absent."""
    if account_id is None:
        return "account_id IS NULL", ()
    return "account_id = ?", (account_id,)


def delete_leads(ids: list[int], account_id: Optional[int] = None) -> int:
    """Delete specific leads by id, scoped to one account so a sub-account
    admin can never delete another account's lead even by guessing an id.
    Returns how many rows were actually removed, so the caller can tell a
    partial delete (some ids did not exist / belonged to someone else) from
    a full one."""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    clause, extra = _account_clause(account_id)
    before = _query(
        f"SELECT COUNT(*) c FROM leads WHERE {clause}", extra
    )[0]["c"]
    _exec(
        f"DELETE FROM leads WHERE id IN ({placeholders}) AND {clause}",
        (*ids, *extra),
    )
    after = _query(
        f"SELECT COUNT(*) c FROM leads WHERE {clause}", extra
    )[0]["c"]
    return before - after


def delete_all_leads(account_id: Optional[int] = None) -> int:
    clause, extra = _account_clause(account_id)
    before = _query(f"SELECT COUNT(*) c FROM leads WHERE {clause}", extra)[0]["c"]
    _exec(f"DELETE FROM leads WHERE {clause}", extra)
    return before


def list_leads(limit: int = 100, account_id: Optional[int] = None) -> list[dict[str, Any]]:
    clause, extra = _account_clause(account_id)
    rows = _query(
        f"SELECT id, session_id, name, email, phone, company, problem, interest, "
        f"qualified, synced, created_at FROM leads WHERE {clause} "
        f"ORDER BY id DESC LIMIT ?",
        (*extra, limit),
    )
    return [dict(r) for r in rows]


def _day_totals(day: str, account_id: Optional[int] = None) -> dict[str, float]:
    clause, extra = _account_clause(account_id)
    row = _query(
        f"SELECT COUNT(*) sessions, COALESCE(SUM(stt_seconds),0) stt, "
        f"COALESCE(SUM(turn_count),0) turns FROM sessions WHERE day = ? AND {clause}",
        (day, *extra),
    )[0]
    leads = _query(
        f"SELECT COUNT(*) c FROM leads WHERE date(created_at, 'unixepoch') = ? "
        f"AND {clause}",
        (day, *extra),
    )[0]["c"]
    return {"sessions": row["sessions"], "stt": row["stt"], "turns": row["turns"],
            "leads": leads}


def usage_summary(account_id: Optional[int] = None) -> dict[str, Any]:
    day = _today()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    today = _day_totals(day, account_id)
    prior = _day_totals(yesterday, account_id)

    clause, extra = _account_clause(account_id)
    total = _query(
        f"SELECT COUNT(*) sessions, COALESCE(SUM(stt_seconds),0) stt "
        f"FROM sessions WHERE {clause}",
        extra,
    )[0]
    leads_total = _query(
        f"SELECT COUNT(*) c FROM leads WHERE {clause}", extra
    )[0]["c"]

    return {
        "day": day,
        "sessions_today": today["sessions"],
        "stt_minutes_today": round(today["stt"] / 60.0, 2),
        "turns_today": today["turns"],
        "leads_today": today["leads"],
        "sessions_total": total["sessions"],
        "stt_hours_total": round(total["stt"] / 3600.0, 2),
        "leads_total": leads_total,
        "deltas": {
            "sessions": _pct_change(today["sessions"], prior["sessions"]),
            "stt": _pct_change(today["stt"], prior["stt"]),
            "turns": _pct_change(today["turns"], prior["turns"]),
            "leads": _pct_change(today["leads"], prior["leads"]),
        },
    }


def _pct_change(today_val: float, prior_val: float) -> Optional[int]:
    """None means 'not meaningful' (no prior-day data to compare against) -
    the template shows no badge rather than a misleading number."""
    if prior_val <= 0:
        return None
    return round((today_val - prior_val) / prior_val * 100)


# --- Settings (admin-editable, no redeploy needed) --------------------------

def get_setting(key: str, default: str = "") -> str:
    rows = _query("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def get_settings(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    rows = _query(f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                 tuple(keys))
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: str) -> None:
    _exec(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- Magic-link login ---------------------------------------------------

def create_login_token(token: str, email: str, ttl_seconds: int = 900) -> None:
    now = time.time()
    _exec(
        "INSERT INTO login_tokens (token, email, expires_at, used, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (token, email, now + ttl_seconds, now),
    )


def consume_login_token(token: str) -> Optional[str]:
    """Returns the associated email if the token is valid and unused, and
    marks it used in the same step so it can never be redeemed twice."""
    rows = _query(
        "SELECT email, expires_at, used FROM login_tokens WHERE token = ?",
        (token,),
    )
    if not rows:
        return None
    row = rows[0]
    if row["used"] or row["expires_at"] < time.time():
        return None
    _exec("UPDATE login_tokens SET used = 1 WHERE token = ?", (token,))
    return row["email"]


# --- Accounts (sub-accounts / businesses trialling their own agent) --------

_SLUG_RE_HINT = "lowercase letters, numbers and hyphens only"


def create_account_application(slug: str, business_name: str, email: str,
                                website: str = "", note: str = "") -> Optional[int]:
    """Returns the new row's id, or None if the slug is already taken."""
    try:
        cur = _exec(
            "INSERT INTO accounts (slug, business_name, website, email, note, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (slug, business_name, website or None, email, note or None, time.time()),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid or 0)


def get_account(account_id: int) -> Optional[dict[str, Any]]:
    rows = _query("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return dict(rows[0]) if rows else None


def get_account_by_slug(slug: str) -> Optional[dict[str, Any]]:
    rows = _query("SELECT * FROM accounts WHERE slug = ?", (slug,))
    return dict(rows[0]) if rows else None


def get_account_by_email(email: str) -> Optional[dict[str, Any]]:
    """Used at admin login: a sub-account's contact email doubles as its
    admin login, the same way ADMIN_EMAILS does for the main account.
    Picks the most recently created match if the same email somehow applied
    more than once."""
    rows = _query(
        "SELECT * FROM accounts WHERE email = ? ORDER BY created_at DESC", (email,)
    )
    return dict(rows[0]) if rows else None


def list_accounts(status: Optional[str] = None) -> list[dict[str, Any]]:
    if status:
        rows = _query(
            "SELECT * FROM accounts WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
    else:
        rows = _query("SELECT * FROM accounts ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def count_approved_accounts() -> int:
    return _query(
        "SELECT COUNT(*) c FROM accounts WHERE status = 'approved'"
    )[0]["c"]


def approve_account(account_id: int, trial_ends_at: Optional[float],
                    daily_minutes_limit: int) -> None:
    _exec(
        "UPDATE accounts SET status = 'approved', trial_ends_at = ?, "
        "daily_minutes_limit = ?, approved_at = ? WHERE id = ?",
        (trial_ends_at, daily_minutes_limit, time.time(), account_id),
    )


def reject_account(account_id: int) -> None:
    _exec("UPDATE accounts SET status = 'rejected' WHERE id = ?", (account_id,))


def delete_account(account_id: int) -> None:
    """Removes the account and everything scoped to it - approving someone by
    mistake should be fully undoable, not just a status flip that leaves
    their data (and their ability to keep using the link) behind."""
    _exec("DELETE FROM accounts WHERE id = ?", (account_id,))
    _exec("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    _exec("DELETE FROM leads WHERE account_id = ?", (account_id,))
    _exec("DELETE FROM turns WHERE account_id = ?", (account_id,))


def set_account_plan(account_id: int, plan_type: str) -> None:
    _exec("UPDATE accounts SET plan_type = ? WHERE id = ?", (plan_type, account_id))


def update_account_plan(account_id: int, plan_type: str,
                        daily_minutes_limit: int,
                        trial_ends_at: Optional[float]) -> None:
    """The manual 'mark as paid' control for /admin/accounts - there is no
    payment gateway wired up yet, so this is how a subscription or one-time
    sale (handled outside the app - UPI, bank transfer, a PayPal.me link,
    whatever) actually takes effect: the owner flips this after being paid."""
    _exec(
        "UPDATE accounts SET plan_type = ?, daily_minutes_limit = ?, "
        "trial_ends_at = ? WHERE id = ?",
        (plan_type, daily_minutes_limit, trial_ends_at, account_id),
    )


def mark_limit_email_sent(account_id: int) -> None:
    _exec(
        "UPDATE accounts SET last_limit_email_at = ? WHERE id = ?",
        (time.time(), account_id),
    )
