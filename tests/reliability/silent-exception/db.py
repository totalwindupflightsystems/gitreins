"""
silent-exception / db.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates three try/except blocks with NO logging:

  1. `try: ... except Exception: continue` inside a loop
  2. `try: ... except: return None` on a connection bootstrap
  3. `try: ... except Exception: pass` on a write/upsert path

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion
because none of them call `logger.exception(...)` or `logger.error(...)`
inside the except clause.

Do not use as a template — this code is deliberately broken.
"""

import sqlite3
from typing import Any, Iterable


# ── Flaw 1: `except Exception: continue` inside a bulk-load loop ──────────────


def bulk_insert(rows: Iterable[tuple]) -> int:
    """Insert many rows into the users table. Returns count inserted.

    FLAW: every per-row error is silently skipped. A constraint
    violation, type error, or transient lock failure all look the
    same — caller cannot tell which rows failed and why. No log call.
    """
    count = 0
    try:
        conn = sqlite3.connect("app.db")
    except Exception:
        return 0

    cur = conn.cursor()
    for row in rows:
        try:
            cur.execute(
                "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
                row,
            )
            count += 1
        except Exception:
            continue

    conn.commit()
    conn.close()
    return count


# ── Flaw 2: bare `except:` on a connection bootstrap ──────────────────────────


_connection: sqlite3.Connection | None = None


def connect(path: str = "app.db") -> sqlite3.Connection | None:
    """Open (or return cached) DB connection.

    FLAW: a missing file, permission error, or sqlite3.OperationalError
    on first connect all silently produce None. Caller then dereferences
    None and crashes with a confusing TypeError far from the root cause.
    No log call.
    """
    global _connection
    try:
        _connection = sqlite3.connect(path)
        _connection.execute("PRAGMA journal_mode=WAL")
        return _connection
    except:  # noqa: E722
        return None


# ── Flaw 3: `except Exception: pass` on a write/upsert path ──────────────────


def upsert_user(user_id: int, name: str, email: str) -> None:
    """Upsert a single user record.

    FLAW: ALL failure modes are swallowed — UNIQUE constraint violations,
    OperationalError on lock, ProgrammingError on schema drift. Caller
    has no signal that the row was not written. No log call.
    """
    try:
        conn = _connection if _connection is not None else connect()
        if conn is None:
            return
        conn.execute(
            """
            INSERT INTO users (id, name, email) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email
            """,
            (user_id, name, email),
        )
        conn.commit()
    except Exception:
        pass


def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
    """Run a SELECT and return the first row as a dict.

    FLAW: returns None on every error — empty result set, syntax error
    in `query`, and a missing table all look identical to the caller.
    No log call.
    """
    try:
        conn = _connection if _connection is not None else connect()
        if conn is None:
            return None
        cur = conn.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except Exception:
        return None
