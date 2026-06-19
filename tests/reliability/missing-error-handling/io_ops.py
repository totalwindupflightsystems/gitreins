"""
missing-error-handling / io_ops.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates the "AI code generator forgets error handling
around I/O, network, and division" anti-pattern. Every function below
performs an operation that can fail (open a file, parse JSON, fetch
a URL, divide by zero, look up a key) but none of them wrap the
operation in `try/except`. The function propagates whatever exception
the interpreter raises — there is no validation, no fallback, no
domain-specific error.

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion
because the operation is unguarded.

Do not use as a template — this code is deliberately broken.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Any


# ── Flaw 1: file open without try/except — FileNotFoundError propagates ──────


def read_text_file(path: str) -> str:
    """Read a UTF-8 text file and return its contents.

    FLAW: `open(path)` is called without a try/except. A missing
    file or permission error propagates as FileNotFoundError /
    PermissionError to the caller. There is no `if not
    os.path.exists(path)` check, no fallback, no domain exception.
    """
    with open(path, "r", encoding="utf-8") as f:    # ← no try/except
        return f.read()


# ── Flaw 2: JSON parse without try/except — json.JSONDecodeError propagates ──


def parse_json(text: str) -> Any:
    """Parse `text` as JSON and return the decoded value.

    FLAW: `json.loads(text)` raises json.JSONDecodeError on bad
    input. The function does not catch it, validate `text` first,
    or wrap the call in try/except.
    """
    return json.loads(text)                          # ← no try/except


# ── Flaw 3: HTTP GET without try/except — URLError propagates ────────────────


def fetch_url(url: str) -> bytes:
    """GET `url` and return the response body as bytes.

    FLAW: `urllib.request.urlopen(url)` raises URLError /
    HTTPError on bad URLs, DNS failures, timeouts, and 4xx/5xx
    responses. The function does not catch any of these or wrap
    them in a domain exception.
    """
    with urllib.request.urlopen(url) as resp:        # ← no try/except
        return resp.read()


# ── Flaw 4: dict access without KeyError handling ────────────────────────────


def get_user_email(users: dict, user_id: int) -> str:
    """Return the email for `user_id` in `users`.

    FLAW: `users[user_id]["email"]` raises KeyError when the user
    is missing or has no email field. There is no `.get(...)`,
    no `if user_id in users:` guard, no try/except.
    """
    return users[user_id]["email"]                   # ← no KeyError handling


# ── Flaw 5: division without ZeroDivisionError check ─────────────────────────


def safe_divide(a: float, b: float) -> float:
    """Return a / b, with a sensible default for zero divisors.

    FLAW: the function name promises safety but the body does
    `a / b` without checking `b == 0`. ZeroDivisionError propagates
    — the function is anything but safe.
    """
    return a / b                                    # ← no b == 0 check


# ── Flaw 6: environment variable lookup without default ──────────────────────


def get_required_env(name: str) -> str:
    """Return the value of the required environment variable `name`.

    FLAW: `os.environ[name]` raises KeyError if the variable is not
    set. The function name promises to return a value but instead
    raises. There is no `os.environ.get(name, default)` and no
    try/except.
    """
    return os.environ[name]                         # ← no .get(...) fallback


# ── Flaw 7: int() conversion without ValueError handling ─────────────────────


def parse_age(raw: str) -> int:
    """Parse `raw` as an integer age.

    FLAW: `int(raw)` raises ValueError on non-numeric input. The
    function does not catch it, does not validate `raw`, and does
    not wrap the call in try/except.
    """
    return int(raw)                                 # ← no try/except