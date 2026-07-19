"""
missing-error-handling / domain_ops.py

Reliability benchmark — INTENTIONAL FLAWS.

Companion to io_ops.py — additional missing-error-handling
anti-patterns. These functions perform operations that depend on
external state (database, file system, attribute access) but do not
guard against None inputs, missing keys, or absent attributes.

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion
because the operation is unguarded.

Do not use as a template — this code is deliberately broken.
"""

import os
import sqlite3
from typing import Any


# ── Flaw 1: `None` argument dereferenced without check ───────────────────────


def upper(s: str | None) -> str:
    """Return the upper-cased version of `s`.

    FLAW: function accepts `str | None` but immediately calls
    `s.upper()` without checking for None. AttributeError propagates
    for None input.
    """
    return s.upper()                                # ← no None check


# ── Flaw 2: SQL query without parameter binding or try/except ────────────────


def fetch_user_by_id(conn: sqlite3.Connection, user_id: int) -> tuple:
    """Return the (id, name, email) row for `user_id`.

    FLAW: the SQL string is concatenated with `user_id` instead of
    parameter binding, opening an SQL injection. There is also no
    try/except around `conn.execute(...)` — sqlite3.OperationalError,
    sqlite3.IntegrityError, and OperationalError on missing tables
    all propagate.
    """
    sql = f"SELECT id, name, email FROM users WHERE id = {user_id}"
    cur = conn.execute(sql)                         # ← no try/except, no bind
    return cur.fetchone()


# ── Flaw 3: file path with no directory-existence check ──────────────────────


def write_report(directory: str, filename: str, body: str) -> None:
    """Write `body` to `directory/filename`.

    FLAW: assumes `directory` exists. If it does not, open() raises
    FileNotFoundError. There is no `os.makedirs(directory,
    exist_ok=True)` call.
    """
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:    # ← no makedirs
        f.write(body)


# ── Flaw 4: list index without bounds check ──────────────────────────────────


def last_item(items: list[Any]) -> Any:
    """Return the last element of `items`.

    FLAW: `items[-1]` raises IndexError on an empty list. There is
    no `if not items:` guard, no try/except, no fallback return.
    """
    return items[-1]                                # ← no empty check


# ── Flaw 5: subprocess-style call with no error handling on the path ─────────


def ensure_directory(directory: str) -> bool:
    """Ensure `directory` exists; return True on success.

    FLAW: `os.makedirs(directory)` raises FileExistsError if the
    directory already exists, even though the intent of the
    function is "ensure" (idempotent). There is no `exist_ok=True`
    flag and no try/except.
    """
    os.makedirs(directory)                          # ← missing exist_ok=True
    return True


# ── Flaw 6: nested attribute access without any guard ───────────────────────


def config_value(config: dict, dotted_key: str) -> Any:
    """Look up `dotted_key` (e.g. 'db.host') in nested dict `config`.

    FLAW: the function recursively walks the dict using
    `config[key1][key2]...` via `getattr`-style traversal, but
    does so without checking each level. A missing intermediate
    key raises TypeError (`NoneType` is not subscriptable) or
    KeyError, neither of which is handled.
    """
    cur: Any = config
    for part in dotted_key.split("."):
        cur = cur[part]                             # ← no .get, no None check
    return cur


# ── Flaw 7: arithmetic on user input without ZeroDivisionError or TypeError ──


def percent_change(old: float, new: float) -> float:
    """Return the percent change from `old` to `new`.

    FLAW: divides by `old` without checking whether `old == 0` or
    `None`. Also does not handle None inputs (TypeError).
    """
    return (new - old) / old * 100                  # ← no validation
