"""SQLite data layer — multi-tenant schema + access functions.

Hierarchy: **organization → workspace**, with **users** belonging to an org.
Usage is metered per (org, workspace, provider, model, task_type). Prepaid
credit is an append-only ledger; the org balance is the sum of its deltas, so it
is always auditable and can never silently drift.

All money is stored in USD as REAL. All timestamps are Unix epoch seconds
(matching ``plutus.py``'s ``state.db`` convention).

The connection uses WAL + a row factory returning ``sqlite3.Row`` so callers get
dict-like rows. Nothing here imports Flask/Stripe — it's pure stdlib and works
fully offline.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    slug               TEXT UNIQUE NOT NULL,
    tier               TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email      TEXT NOT NULL,
    name       TEXT,
    role       TEXT NOT NULL DEFAULT 'owner',
    created_at REAL NOT NULL,
    UNIQUE(org_id, email)
);

CREATE TABLE IF NOT EXISTS workspaces (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    slug               TEXT NOT NULL,
    monthly_budget_usd REAL,
    created_at         REAL NOT NULL,
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id      TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    provider          TEXT NOT NULL,
    model             TEXT,
    task_type         TEXT NOT NULL DEFAULT 'general',
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    estimated         INTEGER NOT NULL DEFAULT 1,
    source            TEXT NOT NULL DEFAULT 'api',
    ts                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_org_ts ON usage_events(org_id, ts);
CREATE INDEX IF NOT EXISTS ix_usage_ws_ts  ON usage_events(workspace_id, ts);
CREATE INDEX IF NOT EXISTS ix_usage_prov   ON usage_events(org_id, provider);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    delta_usd     REAL NOT NULL,           -- +topup/grant/refund, -debit
    kind          TEXT NOT NULL,           -- topup|grant|debit|refund|adjust
    reason        TEXT,
    stripe_ref    TEXT,
    balance_after REAL NOT NULL,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_org_ts ON credit_ledger(org_id, ts);

CREATE TABLE IF NOT EXISTS alerts_log (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id TEXT,
    kind         TEXT NOT NULL,            -- low_balance|budget_warn|budget_cap
    message      TEXT NOT NULL,
    delivered    INTEGER NOT NULL DEFAULT 0,
    ts           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,         -- idempotency: never process twice
    type         TEXT NOT NULL,
    processed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,        -- opaque random; lives in an HttpOnly cookie
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,        -- key_...
    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name         TEXT,
    prefix       TEXT NOT NULL,           -- shown in the UI, e.g. plutus_sk_AbC1
    token_hash   TEXT NOT NULL UNIQUE,    -- sha256 of the full secret; raw never stored
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_apikeys_org  ON api_keys(org_id);
CREATE INDEX IF NOT EXISTS ix_apikeys_hash ON api_keys(token_hash);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Public prefix for ingest API keys. The secret is `plutus_sk_<random>`; only its
# sha256 is ever stored, and only the first few chars are kept for display.
API_KEY_PREFIX = "plutus_sk_"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def slugify(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in name.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "default"


# ------------------------------------------------------------- connection ----
def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    from . import config
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


# ---------------------------------------------------------- organizations ----
def create_org(conn, name: str, tier: str = "free",
               owner_email: Optional[str] = None,
               owner_name: Optional[str] = None) -> sqlite3.Row:
    oid = new_id("org")
    slug = slugify(name)
    # ensure slug uniqueness
    n, base = 1, slug
    while conn.execute("SELECT 1 FROM organizations WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    conn.execute(
        "INSERT INTO organizations(id,name,slug,tier,created_at) VALUES(?,?,?,?,?)",
        (oid, name, slug, tier, time.time()),
    )
    if owner_email:
        conn.execute(
            "INSERT INTO users(id,org_id,email,name,role,created_at) VALUES(?,?,?,?,?,?)",
            (new_id("usr"), oid, owner_email, owner_name, "owner", time.time()),
        )
    conn.commit()
    return get_org(conn, oid)


def get_org(conn, org_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()


def get_org_by_slug(conn, slug: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM organizations WHERE slug=?", (slug,)).fetchone()


def list_orgs(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM organizations ORDER BY created_at").fetchall()


def set_org_tier(conn, org_id: str, tier: str) -> None:
    conn.execute("UPDATE organizations SET tier=? WHERE id=?", (tier, org_id))
    conn.commit()


def set_stripe_customer(conn, org_id: str, customer_id: str) -> None:
    conn.execute("UPDATE organizations SET stripe_customer_id=? WHERE id=?",
                 (customer_id, org_id))
    conn.commit()


def org_by_stripe_customer(conn, customer_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM organizations WHERE stripe_customer_id=?", (customer_id,)
    ).fetchone()


# ------------------------------------------------------------------- users ---
def get_user(conn, user_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def users_by_email(conn, email: str) -> list[sqlite3.Row]:
    """All user rows for an email (a person may belong to several orgs)."""
    return conn.execute(
        "SELECT * FROM users WHERE lower(email)=lower(?) ORDER BY created_at", (email,)
    ).fetchall()


def ensure_user(conn, org_id: str, email: str, name: Optional[str] = None,
                role: str = "member") -> sqlite3.Row:
    """Get-or-create the user row for (org, email); backfill name if newly known."""
    row = conn.execute(
        "SELECT * FROM users WHERE org_id=? AND lower(email)=lower(?)",
        (org_id, email),
    ).fetchone()
    if row:
        if name and not row["name"]:
            conn.execute("UPDATE users SET name=? WHERE id=?", (name, row["id"]))
            conn.commit()
            return get_user(conn, row["id"])
        return row
    uid = new_id("usr")
    conn.execute(
        "INSERT INTO users(id,org_id,email,name,role,created_at) VALUES(?,?,?,?,?,?)",
        (uid, org_id, email, name, role, time.time()),
    )
    conn.commit()
    return get_user(conn, uid)


def list_orgs_for_email(conn, email: str) -> list[sqlite3.Row]:
    """Orgs the email is a member of, ordered by org creation."""
    return conn.execute(
        "SELECT o.* FROM organizations o JOIN users u ON u.org_id=o.id "
        "WHERE lower(u.email)=lower(?) ORDER BY o.created_at",
        (email,),
    ).fetchall()


def email_in_org(conn, email: str, org_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM users WHERE org_id=? AND lower(email)=lower(?)",
        (org_id, email),
    ).fetchone() is not None


# ---------------------------------------------------------------- sessions ---
def create_session(conn, user_id: str, ttl_seconds: float) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, user_id, now, now + ttl_seconds),
    )
    conn.commit()
    return token


def session_user(conn, token: str) -> Optional[sqlite3.Row]:
    """Resolve a session token to its user row, or None if missing/expired."""
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND s.expires_at > ?",
        (token, time.time()),
    ).fetchone()


def delete_session(conn, token: str) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()


def purge_expired_sessions(conn) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- api keys ----
def _hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_api_key(conn, org_id: str, name: Optional[str] = None) -> tuple[sqlite3.Row, str]:
    """Mint an ingest API key for an org.

    Returns ``(row, secret)``. The full ``secret`` is shown to the caller **once**
    — only its hash is stored, so it can never be recovered later.
    """
    secret = API_KEY_PREFIX + secrets.token_urlsafe(24)
    kid = new_id("key")
    prefix = secret[:len(API_KEY_PREFIX) + 4]   # e.g. "plutus_sk_AbC1"
    conn.execute(
        "INSERT INTO api_keys(id,org_id,name,prefix,token_hash,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (kid, org_id, name, prefix, _hash_token(secret), time.time()),
    )
    conn.commit()
    return get_api_key(conn, kid), secret


def get_api_key(conn, key_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()


def list_api_keys(conn, org_id: str, include_revoked: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM api_keys WHERE org_id=?"
    if not include_revoked:
        q += " AND revoked_at IS NULL"
    return conn.execute(q + " ORDER BY created_at DESC", (org_id,)).fetchall()


def revoke_api_key(conn, key_id: str, org_id: Optional[str] = None) -> bool:
    """Revoke a key (optionally scoped to an org). Returns True if one changed."""
    if org_id:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE id=? AND org_id=? AND revoked_at IS NULL",
            (time.time(), key_id, org_id))
    else:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (time.time(), key_id))
    conn.commit()
    return cur.rowcount > 0


def api_key_org(conn, secret: str) -> Optional[str]:
    """Resolve a presented API-key secret to its org_id, or None.

    Touches ``last_used_at`` on success. Revoked keys never resolve.
    """
    if not secret or not secret.startswith(API_KEY_PREFIX):
        return None
    row = conn.execute(
        "SELECT * FROM api_keys WHERE token_hash=? AND revoked_at IS NULL",
        (_hash_token(secret),),
    ).fetchone()
    if not row:
        return None
    conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                 (time.time(), row["id"]))
    conn.commit()
    return row["org_id"]


# ------------------------------------------------------------- workspaces ----
def create_workspace(conn, org_id: str, name: str,
                     monthly_budget_usd: Optional[float] = None) -> sqlite3.Row:
    wid = new_id("ws")
    slug = slugify(name)
    n, base = 1, slug
    while conn.execute(
        "SELECT 1 FROM workspaces WHERE org_id=? AND slug=?", (org_id, slug)
    ).fetchone():
        n += 1
        slug = f"{base}-{n}"
    conn.execute(
        "INSERT INTO workspaces(id,org_id,name,slug,monthly_budget_usd,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (wid, org_id, name, slug, monthly_budget_usd, time.time()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()


def list_workspaces(conn, org_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM workspaces WHERE org_id=? ORDER BY created_at", (org_id,)
    ).fetchall()


def get_workspace(conn, workspace_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()


# ----------------------------------------------------------------- credit ----
def get_balance(conn, org_id: str) -> float:
    """Authoritative balance = sum of all ledger deltas.

    Computed from the deltas rather than the latest row's ``balance_after`` so it
    is correct regardless of insertion / timestamp order (live metering arrives
    in order; demo seeding and historical back-fill do not). ``balance_after``
    remains a best-effort audit column.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_usd),0) bal FROM credit_ledger WHERE org_id=?",
        (org_id,),
    ).fetchone()
    return round(float(row["bal"]), 6)


def add_ledger(conn, org_id: str, delta_usd: float, kind: str,
               reason: str = "", stripe_ref: Optional[str] = None,
               ts: Optional[float] = None) -> sqlite3.Row:
    ts = ts if ts is not None else time.time()
    balance_after = round(get_balance(conn, org_id) + delta_usd, 6)
    lid = new_id("led")
    conn.execute(
        "INSERT INTO credit_ledger(id,org_id,delta_usd,kind,reason,stripe_ref,balance_after,ts)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (lid, org_id, round(delta_usd, 6), kind, reason, stripe_ref, balance_after, ts),
    )
    conn.commit()
    return conn.execute("SELECT * FROM credit_ledger WHERE id=?", (lid,)).fetchone()


def ledger_history(conn, org_id: str, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM credit_ledger WHERE org_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
        (org_id, limit),
    ).fetchall()


# ------------------------------------------------------------- stripe idemp ---
def stripe_event_seen(conn, event_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM stripe_events WHERE event_id=?", (event_id,)
    ).fetchone() is not None


def mark_stripe_event(conn, event_id: str, type_: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO stripe_events(event_id,type,processed_at) VALUES(?,?,?)",
        (event_id, type_, time.time()),
    )
    conn.commit()


# -------------------------------------------------------------- alerts log ---
def log_alert(conn, org_id: str, kind: str, message: str,
              workspace_id: Optional[str] = None, delivered: bool = False) -> sqlite3.Row:
    aid = new_id("alr")
    conn.execute(
        "INSERT INTO alerts_log(id,org_id,workspace_id,kind,message,delivered,ts)"
        " VALUES(?,?,?,?,?,?,?)",
        (aid, org_id, workspace_id, kind, message, int(delivered), time.time()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM alerts_log WHERE id=?", (aid,)).fetchone()


def recent_alerts(conn, org_id: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts_log WHERE org_id=? ORDER BY ts DESC LIMIT ?",
        (org_id, limit),
    ).fetchall()
