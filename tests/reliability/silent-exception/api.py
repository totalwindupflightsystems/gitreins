"""
silent-exception / api.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates three silent-exception anti-patterns that the
GitReins reliability benchmark uses to verify the evaluator can detect
swallowed exceptions:

  1. bare `except:` followed by `pass` / returning a sentinel value
  2. `except Exception: pass` with no logging or re-raise
  3. functions that catch exceptions and convert them to a falsy default
     without surfacing the error

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion.

Do not use as a template — this code is deliberately broken.
"""

import json
import urllib.request
import urllib.error


# ── Flaw 1: bare `except:` followed by `pass` ────────────────────────────────


def fetch_user(user_id: int) -> dict | None:
    """Fetch a user record. Returns None on any failure.

    FLAW: bare `except:` silently swallows network errors, JSON decode
    errors, and KeyError. Caller cannot distinguish 'not found' from
    'service down'. No log line is emitted.
    """
    url = f"https://api.example.com/users/{user_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except:  # noqa: E722
        pass


# ── Flaw 2: `except Exception: pass` with no logging ─────────────────────────


def delete_user(user_id: int) -> None:
    """Delete a user record. Idempotent — swallows all errors.

    FLAW: `except Exception: pass` discards every failure mode. A 401
    auth error, 500 server error, or network timeout all look identical
    to the caller (no-op). No log, no re-raise, no metric.
    """
    url = f"https://api.example.com/users/{user_id}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return
    except Exception:
        pass


# ── Flaw 3: catch and convert to a falsy default without re-raise ────────────


def parse_payload(raw: bytes) -> dict:
    """Parse an inbound JSON payload. Returns {} on any parse failure.

    FLAW: converts ParseError into an empty dict and returns it as if
    the payload were valid. Downstream code receives `{}` and cannot
    tell whether the upstream sent empty data or whether the parser
    blew up. No log line, no re-raise, no structured error.
    """
    try:
        decoded = raw.decode("utf-8")
    except Exception:
        return {}
    try:
        return json.loads(decoded)
    except Exception:
        return {}


def update_user_email(user_id: int, new_email: str) -> bool:
    """PATCH the email field. Returns True on success.

    FLAW: returns False on every error path with no log entry and no
    distinction between validation errors and transport errors.
    """
    payload = json.dumps({"email": new_email}).encode("utf-8")
    url = f"https://api.example.com/users/{user_id}"
    req = urllib.request.Request(
        url, data=payload, method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except urllib.error.URLError:
        return False
    except Exception:
        return False
