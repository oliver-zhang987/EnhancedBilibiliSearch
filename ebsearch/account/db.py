"""sqlite access layer for the account subsystem.

Design notes
------------
* The DB file is shared by TWO FastAPI processes mounted on the same host, so
  we cannot rely on a single in-process connection guarding writes. Instead we
  use WAL mode (concurrent readers + one writer) plus SHORT transactions and a
  retry loop on ``database is locked``.
* Connections are opened per-call (``_connect``). sqlite connections are cheap
  and this sidesteps cross-thread / cross-process sharing hazards. We still pass
  ``check_same_thread=False`` defensively.
* ALL queries are parameterized — user input is never string-formatted into SQL.

Every public helper accepts an optional ``conn`` so callers (e.g. credits.py)
can compose several statements inside ONE transaction for atomicity.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

from . import config

# Busy timeout (ms) handed to sqlite, plus an explicit app-level retry loop for
# the rare "database is locked" that slips past busy_timeout under WAL.
_BUSY_TIMEOUT_MS = 5000
_MAX_RETRIES = 6
_RETRY_BASE_SLEEP = 0.05  # seconds; exponential backoff


def _db_path() -> str:
    return config.SETTINGS.db_path


def _connect() -> sqlite3.Connection:
    """Open a fresh connection with WAL + sane pragmas."""
    path = _db_path()
    # ``isolation_level=None`` => autocommit; we open explicit transactions with
    # BEGIN when we need atomicity (see ``transaction``).
    conn = sqlite3.connect(
        path,
        timeout=_BUSY_TIMEOUT_MS / 1000.0,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def get_conn(existing: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection. If ``existing`` is given, reuse it and do NOT close
    it (the outer owner manages its lifecycle)."""
    if existing is not None:
        yield existing
        return
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(existing: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    """Run a block inside an IMMEDIATE transaction with locked-retry.

    On ``database is locked`` the whole block is retried with exponential
    backoff. Nesting (passing ``existing``) joins the outer transaction and does
    not BEGIN/COMMIT again.
    """
    if existing is not None:
        # Already inside a transaction owned by the caller.
        yield existing
        return

    attempt = 0
    while True:
        conn = _connect()
        try:
            # IMMEDIATE acquires the write lock up front, avoiding upgrade
            # deadlocks between the two processes.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            if "locked" in str(exc).lower() and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BASE_SLEEP * (2 ** attempt))
                attempt += 1
                continue
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()


def _execute(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> sqlite3.Cursor:
    """Execute with a small retry loop for transient locks (read paths)."""
    attempt = 0
    while True:
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BASE_SLEEP * (2 ** attempt))
                attempt += 1
                continue
            raise


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    phone_hash  TEXT UNIQUE,
    phone_last4 TEXT,
    credits     INTEGER NOT NULL DEFAULT 0,
    invite_code TEXT,
    consented   INTEGER DEFAULT 0,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code        TEXT PRIMARY KEY,
    credits     INTEGER,
    max_uses    INTEGER,
    used_count  INTEGER DEFAULT 0,
    note        TEXT,
    active      INTEGER DEFAULT 1,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS credits_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    delta        INTEGER,
    balance_after INTEGER,
    reason       TEXT,
    job_id       TEXT,
    ts           TEXT
);

CREATE TABLE IF NOT EXISTS otp (
    phone_hash      TEXT PRIMARY KEY,
    code_hash       TEXT,
    expires_at      REAL,
    attempts        INTEGER DEFAULT 0,
    last_sent       REAL,
    sent_count_hour INTEGER DEFAULT 0,
    hour_window     REAL
);

CREATE INDEX IF NOT EXISTS idx_ledger_user ON credits_ledger(user_id);
"""


def init_db(existing: Optional[sqlite3.Connection] = None) -> None:
    """Create tables/indexes if they do not exist. Idempotent."""
    # Ensure the parent directory exists (shared mounted volume / local dev).
    parent = os.path.dirname(_db_path())
    if parent:
        os.makedirs(parent, exist_ok=True)
    with get_conn(existing) as conn:
        conn.executescript(_SCHEMA)


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def get_user_by_id(
    user_id: int, conn: Optional[sqlite3.Connection] = None
) -> Optional[sqlite3.Row]:
    with get_conn(conn) as c:
        cur = _execute(c, "SELECT * FROM users WHERE id = ?", (user_id,))
        return cur.fetchone()


def get_user_by_phone_hash(
    phone_hash: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[sqlite3.Row]:
    with get_conn(conn) as c:
        cur = _execute(c, "SELECT * FROM users WHERE phone_hash = ?", (phone_hash,))
        return cur.fetchone()


def create_user(
    phone_hash: Optional[str],
    phone_last4: Optional[str],
    credits: int,
    invite_code: Optional[str],
    consented: bool,
    created_at: str,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Insert a user and return the new id. ``phone_hash``/``phone_last4`` are
    NULL for anonymous (access-code-only) accounts. Caller normally runs this
    inside a transaction together with invite-code consumption + ledger write."""
    with get_conn(conn) as c:
        cur = _execute(
            c,
            "INSERT INTO users (phone_hash, phone_last4, credits, invite_code, "
            "consented, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (phone_hash, phone_last4, credits, invite_code, 1 if consented else 0, created_at),
        )
        return int(cur.lastrowid)


def set_user_credits(
    user_id: int, credits: int, conn: Optional[sqlite3.Connection] = None
) -> None:
    with get_conn(conn) as c:
        _execute(c, "UPDATE users SET credits = ? WHERE id = ?", (credits, user_id))


# --------------------------------------------------------------------------- #
# invite_codes
# --------------------------------------------------------------------------- #
def get_invite_code(
    code: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[sqlite3.Row]:
    with get_conn(conn) as c:
        cur = _execute(c, "SELECT * FROM invite_codes WHERE code = ?", (code,))
        return cur.fetchone()


def insert_invite_code(
    code: str,
    credits: int,
    max_uses: int,
    note: Optional[str],
    created_at: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    with get_conn(conn) as c:
        _execute(
            c,
            "INSERT INTO invite_codes (code, credits, max_uses, used_count, note, "
            "active, created_at) VALUES (?, ?, ?, 0, ?, 1, ?)",
            (code, credits, max_uses, note, created_at),
        )


# --------------------------------------------------------------------------- #
# credits_ledger
# --------------------------------------------------------------------------- #
def insert_ledger(
    user_id: int,
    delta: int,
    balance_after: int,
    reason: str,
    job_id: Optional[str],
    ts: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    with get_conn(conn) as c:
        _execute(
            c,
            "INSERT INTO credits_ledger (user_id, delta, balance_after, reason, "
            "job_id, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, delta, balance_after, reason, job_id, ts),
        )


# --------------------------------------------------------------------------- #
# otp
# --------------------------------------------------------------------------- #
def get_otp(
    phone_hash: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[sqlite3.Row]:
    with get_conn(conn) as c:
        cur = _execute(c, "SELECT * FROM otp WHERE phone_hash = ?", (phone_hash,))
        return cur.fetchone()


def upsert_otp(
    phone_hash: str,
    code_hash: str,
    expires_at: float,
    last_sent: float,
    sent_count_hour: int,
    hour_window: float,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Insert or replace an OTP row, resetting attempts to 0 on each send."""
    with get_conn(conn) as c:
        _execute(
            c,
            "INSERT INTO otp (phone_hash, code_hash, expires_at, attempts, "
            "last_sent, sent_count_hour, hour_window) VALUES (?, ?, ?, 0, ?, ?, ?) "
            "ON CONFLICT(phone_hash) DO UPDATE SET "
            "code_hash=excluded.code_hash, expires_at=excluded.expires_at, "
            "attempts=0, last_sent=excluded.last_sent, "
            "sent_count_hour=excluded.sent_count_hour, "
            "hour_window=excluded.hour_window",
            (phone_hash, code_hash, expires_at, last_sent, sent_count_hour, hour_window),
        )


def increment_otp_attempts(
    phone_hash: str, conn: Optional[sqlite3.Connection] = None
) -> None:
    with get_conn(conn) as c:
        _execute(
            c, "UPDATE otp SET attempts = attempts + 1 WHERE phone_hash = ?", (phone_hash,)
        )


def delete_otp(phone_hash: str, conn: Optional[sqlite3.Connection] = None) -> None:
    with get_conn(conn) as c:
        _execute(c, "DELETE FROM otp WHERE phone_hash = ?", (phone_hash,))
