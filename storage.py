"""
storage.py — persistence layer for the Vera bot.

Why SQLite instead of a plain in-memory dict:
    The reference skeleton in the testing brief uses a bare Python dict. That
    works, but a single dropped/restarted process during a 60-minute live
    test would silently wipe every pushed context and every conversation —
    exactly the failure mode the brief warns about ("just don't restart
    between calls"). A tiny SQLite file survives process restarts on the
    same host for free, costs nothing in latency (WAL mode, local disk),
    and needs zero extra infrastructure to deploy.

    Run the bot as a SINGLE worker process (see Procfile / README) so this
    store is never accessed from two processes with two different SQLite
    connections at once. Threads are fine — every connection here is
    check_same_thread=False and guarded by one process-wide lock.

Tables:
    contexts       — idempotent (scope, context_id) -> latest version+payload
    conversations  — one row per conversation_id, tracks state machine fields
    turns          — full transcript, both directions
    suppression    — trigger suppression_key dedup (don't resend same key)
    merchant_state — small per-merchant counters (unanswered-nudge streak)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

DB_PATH = os.environ.get("VERA_DB_PATH", os.path.join(os.path.dirname(__file__), "vera_state.db"))

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contexts (
                scope TEXT NOT NULL,
                context_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                delivered_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (scope, context_id)
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                merchant_id TEXT,
                customer_id TEXT,
                trigger_id TEXT,
                created_at TEXT,
                ended INTEGER DEFAULT 0,
                turn_count INTEGER DEFAULT 0,
                last_outbound_at TEXT,
                last_inbound_at TEXT,
                auto_reply_hits INTEGER DEFAULT 0,
                intent_committed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                from_role TEXT,
                body TEXT,
                ts TEXT
            );

            CREATE TABLE IF NOT EXISTS suppression (
                suppression_key TEXT PRIMARY KEY,
                sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS merchant_state (
                merchant_id TEXT PRIMARY KEY,
                unanswered_streak INTEGER DEFAULT 0,
                auto_reply_hits INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id);
            """
        )
        conn.commit()


def wipe() -> None:
    """Used by the optional /v1/teardown endpoint."""
    with _lock:
        conn = get_conn()
        for t in ("contexts", "conversations", "turns", "suppression", "merchant_state"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------

def push_context(scope: str, context_id: str, version: int, payload: dict, delivered_at: str = "") -> dict:
    """Idempotent by (scope, context_id, version). Returns the ack/err dict."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT version FROM contexts WHERE scope=? AND context_id=?", (scope, context_id)
        ).fetchone()
        if row and row["version"] >= version:
            return {"accepted": False, "reason": "stale_version", "current_version": row["version"]}
        conn.execute(
            """INSERT INTO contexts (scope, context_id, version, payload, delivered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, context_id) DO UPDATE SET
                 version=excluded.version, payload=excluded.payload,
                 delivered_at=excluded.delivered_at, updated_at=excluded.updated_at""",
            (scope, context_id, version, json.dumps(payload), delivered_at, _now_iso()),
        )
        conn.commit()
        return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": _now_iso()}


def get_context(scope: str, context_id: str) -> Optional[dict]:
    if not context_id:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT payload FROM contexts WHERE scope=? AND context_id=?", (scope, context_id)
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def find_merchant_by_id(merchant_id: str) -> Optional[dict]:
    return get_context("merchant", merchant_id)


def find_category_for_merchant(merchant: dict) -> Optional[dict]:
    if not merchant:
        return None
    slug = merchant.get("category_slug")
    return get_context("category", slug) if slug else None


def contexts_loaded_counts() -> dict:
    conn = get_conn()
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for row in conn.execute("SELECT scope, COUNT(*) AS n FROM contexts GROUP BY scope"):
        counts[row["scope"]] = row["n"]
    return counts


def all_trigger_ids() -> list[str]:
    conn = get_conn()
    return [r["context_id"] for r in conn.execute("SELECT context_id FROM contexts WHERE scope='trigger'")]


# ---------------------------------------------------------------------------
# Suppression (trigger-level dedup)
# ---------------------------------------------------------------------------

def is_suppressed(key: str) -> bool:
    if not key:
        return False
    conn = get_conn()
    return conn.execute("SELECT 1 FROM suppression WHERE suppression_key=?", (key,)).fetchone() is not None


def mark_suppressed(key: str) -> None:
    if not key:
        return
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO suppression (suppression_key, sent_at) VALUES (?, ?)",
            (key, _now_iso()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Conversations / turns
# ---------------------------------------------------------------------------

def create_conversation(conversation_id: str, merchant_id: str, customer_id: Optional[str], trigger_id: Optional[str]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO conversations
               (conversation_id, merchant_id, customer_id, trigger_id, created_at, last_outbound_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conversation_id, merchant_id, customer_id, trigger_id, _now_iso(), _now_iso()),
        )
        conn.commit()


def get_conversation(conversation_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    return dict(row) if row else None


def record_turn(conversation_id: str, from_role: str, body: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO turns (conversation_id, from_role, body, ts) VALUES (?, ?, ?, ?)",
            (conversation_id, from_role, body, _now_iso()),
        )
        field = "last_inbound_at" if from_role in ("merchant", "customer") else "last_outbound_at"
        conn.execute(
            f"UPDATE conversations SET turn_count = turn_count + 1, {field} = ? WHERE conversation_id=?",
            (_now_iso(), conversation_id),
        )
        conn.commit()


def get_turns(conversation_id: str) -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT from_role, body, ts FROM turns WHERE conversation_id=? ORDER BY id ASC", (conversation_id,)
    )]


def set_auto_reply_hits(conversation_id: str, n: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE conversations SET auto_reply_hits=? WHERE conversation_id=?", (n, conversation_id))
        conn.commit()


def set_intent_committed(conversation_id: str, val: bool = True) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE conversations SET intent_committed=? WHERE conversation_id=?", (1 if val else 0, conversation_id))
        conn.commit()


def end_conversation(conversation_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE conversations SET ended=1 WHERE conversation_id=?", (conversation_id,))
        conn.commit()


def recent_bodies_for(merchant_id: str, customer_id: Optional[str], limit: int = 20) -> list[str]:
    """Anti-repetition lookback across ALL conversations for this merchant/customer pair."""
    conn = get_conn()
    if customer_id:
        rows = conn.execute(
            """SELECT t.body FROM turns t JOIN conversations c ON t.conversation_id = c.conversation_id
               WHERE c.merchant_id=? AND c.customer_id=? AND t.from_role IN ('vera','merchant_on_behalf')
               ORDER BY t.id DESC LIMIT ?""",
            (merchant_id, customer_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT t.body FROM turns t JOIN conversations c ON t.conversation_id = c.conversation_id
               WHERE c.merchant_id=? AND (c.customer_id IS NULL) AND t.from_role IN ('vera','merchant_on_behalf')
               ORDER BY t.id DESC LIMIT ?""",
            (merchant_id, limit),
        ).fetchall()
    return [r["body"] for r in rows]


# ---------------------------------------------------------------------------
# Merchant-level "knowing when to stop" counter
# ---------------------------------------------------------------------------

def get_unanswered_streak(merchant_id: str) -> int:
    conn = get_conn()
    row = conn.execute("SELECT unanswered_streak FROM merchant_state WHERE merchant_id=?", (merchant_id,)).fetchone()
    return row["unanswered_streak"] if row else 0


def incr_unanswered(merchant_id: str) -> int:
    with _lock:
        conn = get_conn()
        cur = get_unanswered_streak(merchant_id) + 1
        conn.execute(
            "INSERT INTO merchant_state (merchant_id, unanswered_streak) VALUES (?, ?) "
            "ON CONFLICT(merchant_id) DO UPDATE SET unanswered_streak=?",
            (merchant_id, cur, cur),
        )
        conn.commit()
        return cur


def reset_unanswered(merchant_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO merchant_state (merchant_id, unanswered_streak) VALUES (?, 0) "
            "ON CONFLICT(merchant_id) DO UPDATE SET unanswered_streak=0",
            (merchant_id,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Merchant-level auto-reply (WhatsApp Business canned-response) detection.
# This is deliberately keyed by merchant, not conversation_id: a canned
# auto-responder is a property of the merchant's WhatsApp number, not of
# any single conversation thread.
# ---------------------------------------------------------------------------

def get_merchant_auto_reply_hits(merchant_id: str) -> int:
    if not merchant_id:
        return 0
    conn = get_conn()
    row = conn.execute("SELECT auto_reply_hits FROM merchant_state WHERE merchant_id=?", (merchant_id,)).fetchone()
    return row["auto_reply_hits"] if row else 0


def incr_merchant_auto_reply_hits(merchant_id: str) -> int:
    if not merchant_id:
        return 0
    with _lock:
        conn = get_conn()
        cur = get_merchant_auto_reply_hits(merchant_id) + 1
        conn.execute(
            "INSERT INTO merchant_state (merchant_id, auto_reply_hits) VALUES (?, ?) "
            "ON CONFLICT(merchant_id) DO UPDATE SET auto_reply_hits=?",
            (merchant_id, cur, cur),
        )
        conn.commit()
        return cur


def reset_merchant_auto_reply_hits(merchant_id: str) -> None:
    if not merchant_id:
        return
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO merchant_state (merchant_id, auto_reply_hits) VALUES (?, 0) "
            "ON CONFLICT(merchant_id) DO UPDATE SET auto_reply_hits=0",
            (merchant_id,),
        )
        conn.commit()
