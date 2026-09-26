"""Per-judgment token and cost attribution for the judgment viewer (JVIEW-006).

``.gitreins/usage.jsonl`` records one telemetry line per evaluation step
(``ts``, ``tokens_in``, ``tokens_out``, ``cache_read``, ``cache_write``,
``step``) and carries no task id, so attribution is by TIME: a line belongs to
the verdict whose ``evaluated_at`` is the earliest one at or after the line's
timestamp. Every line is therefore charged to at most one verdict — the viewer
never double-counts a judge run across two rows — and a line that precedes no
verdict (a pre-commit judge pass, an evaluation whose verdict was never
persisted) stays unattributed instead of being blamed on an unrelated verdict.

Costs come from the checkout's own price configuration, because a token count is
a measurement and a price is a setting:

.. code-block:: yaml

    usage:
      model: deepseek-v4-flash        # optional; defaults to defaults.model
      price_per_1m_input: 0.28        # USD per 1M input tokens
      price_per_1m_output: 0.42       # USD per 1M output tokens

With no prices configured the reader reports the tokens and ``priced: False``;
the viewer then shows the counts and says the cost is unpriced instead of
inventing a rate for a provider's current price list. ``tokens_in`` already
includes cache reads (per the telemetry contract), so a cost is charged on
``tokens_in`` and ``tokens_out`` only — ``cache_read``/``cache_write`` are
carried alongside for the reader's own arithmetic.

The write side lives here too (:func:`append_usage_row`), next to the reader that
has to parse the schema: the judge pipeline appends its ``tier2`` step inline
(``engine/pipeline.py``) and the resolution gate appends its ``resolution`` step
through this helper, both into the SAME file, so one module owns the shape.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

USAGE_FILE = "usage.jsonl"
CONFIG_FILE = "config.yaml"

DEFAULT_PRICE_CONFIG: dict[str, Any] = {
    "model": "",
    "price_per_1m_input": 0.0,
    "price_per_1m_output": 0.0,
}

_TOKENS = ("tokens_in", "tokens_out", "cache_read", "cache_write")


def usage_path(workdir: str) -> str:
    """``<workdir>/.gitreins/usage.jsonl`` — the judge telemetry file."""
    return os.path.join(workdir, ".gitreins", USAGE_FILE)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _counter(value: Any) -> int:
    """A token count as the schema stores it: a non-negative int, never a bool."""
    number = _as_number(value)
    if number is None or number < 0:
        return 0
    return int(number)


def append_usage_row(
    workdir: str,
    *,
    step: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    ts: float | None = None,
) -> bool:
    """Append one telemetry row to ``<workdir>/.gitreins/usage.jsonl``.

    The single writer of the schema described in this module's docstring: the
    row it writes is byte-for-byte the shape :func:`load_usage_rows` and
    :func:`attribute_rows` read, so a new producer (the resolution gate's
    ``resolution`` step) cannot drift from the judge's ``tier2`` rows.

    Best-effort by contract, exactly like the judge's inline append: an
    unwritable directory, a full disk or a bad value returns ``False`` and is
    never raised into the run that produced the tokens. Returns ``True`` when
    the line reached the file.

    ``ts`` defaults to now. A caller that already measured the moment passes it,
    because attribution is BY TIME (:func:`attribute_rows`): the row must land
    before the verdict it belongs to, never after it.
    """
    row = {
        "ts": time.time() if ts is None else _as_number(ts) or time.time(),
        "tokens_in": _counter(tokens_in),
        "tokens_out": _counter(tokens_out),
        "cache_read": _counter(cache_read),
        "cache_write": _counter(cache_write),
        "step": step,
    }
    try:
        path = usage_path(workdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except Exception:
        return False
    return True


def load_usage_rows(workdir: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Usage rows with a numeric ``ts``, oldest first; malformed lines skipped.

    A missing or unreadable file is an empty list — the same best-effort
    contract the telemetry writer holds (absence is not evidence of no judge
    run, and it is never an error).
    """
    path = usage_path(workdir)
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or _as_number(row.get("ts")) is None:
            continue
        rows.append(row)
    rows.sort(key=lambda row: float(row["ts"]))
    return rows


def load_price_config(workdir: str) -> dict[str, Any]:
    """The ``usage:`` block of ``.gitreins/config.yaml``, merged with defaults.

    ``model`` falls back to ``defaults.model`` so a checkout that sets only the
    prices still names the model the prices belong to.
    """
    config = dict(DEFAULT_PRICE_CONFIG)
    raw: dict[str, Any] = {}
    try:
        import yaml

        with open(os.path.join(workdir, ".gitreins", CONFIG_FILE), encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception:
        raw = {}
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if isinstance(usage, dict):
        for key in ("model", "price_per_1m_input", "price_per_1m_output"):
            if key in usage:
                config[key] = usage[key]
    if not config["model"]:
        defaults = raw.get("defaults") if isinstance(raw, dict) else None
        if isinstance(defaults, dict) and defaults.get("model"):
            config["model"] = str(defaults["model"])
    for key in ("price_per_1m_input", "price_per_1m_output"):
        number = _as_number(config[key])
        config[key] = number if number is not None else 0.0
    config["model"] = str(config["model"] or "")
    return config


def prices_configured(prices: dict[str, Any]) -> bool:
    """True when the checkout names at least one non-zero rate."""
    return bool(prices.get("price_per_1m_input") or prices.get("price_per_1m_output"))


def cost_usd(tokens_in: int, tokens_out: int, prices: dict[str, Any]) -> float | None:
    """USD for one judgement, or ``None`` when the checkout configures no rate."""
    if not prices_configured(prices):
        return None
    billed = tokens_in * float(prices.get("price_per_1m_input") or 0.0)
    billed += tokens_out * float(prices.get("price_per_1m_output") or 0.0)
    return round(billed / 1_000_000, 6)


def _tokens(row: dict[str, Any], key: str) -> int:
    value = _as_number(row.get(key))
    return int(value) if value and value > 0 else 0


def attribute_rows(
    stamps: list[tuple[str, str, float]],
    rows: list[dict[str, Any]],
    prices: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """``{"<date>/<hash>": entry}`` — each usage row charged to at most one verdict.

    ``stamps`` is ``(date, hash, evaluated_at_epoch)`` per verdict, in any order;
    the earliest stamp at or after a row's ``ts`` owns it. Verdicts with no rows
    produce no entry, so a caller can distinguish "no judge telemetry" from
    "telemetry says zero".
    """
    prices = dict(DEFAULT_PRICE_CONFIG if prices is None else prices)
    ordered = sorted((s for s in stamps if _as_number(s[2]) is not None), key=lambda s: s[2])
    entries: dict[str, dict[str, Any]] = {}
    for row in rows:
        ts = _as_number(row.get("ts"))
        if ts is None:
            continue
        owner = next((stamp for stamp in ordered if float(stamp[2]) >= ts), None)
        if owner is None:
            continue
        key = f"{owner[0]}/{owner[1]}"
        entry = entries.setdefault(
            key,
            {
                "date": owner[0],
                "hash": owner[1],
                "evaluated_at": float(owner[2]),
                "rows": 0,
                "steps": [],
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "first_ts": ts,
                "last_ts": ts,
                "model": prices.get("model", ""),
            },
        )
        entry["rows"] += 1
        step = row.get("step")
        if isinstance(step, str) and step and step not in entry["steps"]:
            entry["steps"].append(step)
        for token_key in _TOKENS:
            entry[token_key] += _tokens(row, token_key)
        entry["first_ts"] = min(entry["first_ts"], ts)
        entry["last_ts"] = max(entry["last_ts"], ts)
    for entry in entries.values():
        entry["cost_usd"] = cost_usd(entry["tokens_in"], entry["tokens_out"], prices)
        entry["priced"] = entry["cost_usd"] is not None
    return entries


def summarize(
    index: dict[str, dict[str, Any]],
    prices: dict[str, Any] | None = None,
    total_verdicts: int | None = None,
) -> dict[str, Any]:
    """Aggregate for the viewer's stats header.

    ``cost_usd`` is the priced subtotal only, and is ``None`` when nothing was
    priced (``priced == 0``) — the same absent-means-absent shape the
    per-verdict usage blocks use; ``unpriced`` counts attributed judgements
    that could not be costed and ``unattributed`` the verdicts with no
    telemetry at all, so a partial total is never presented as the whole truth.
    """
    prices = dict(DEFAULT_PRICE_CONFIG if prices is None else prices)
    verdicts = len(index) if total_verdicts is None else max(total_verdicts, len(index))
    total: dict[str, Any] = {
        "judgements": len(index),
        "verdicts": verdicts,
        "unattributed": verdicts - len(index),
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost_usd": None,
        "priced": 0,
        "unpriced": 0,
        "model": prices.get("model", ""),
        "prices_configured": prices_configured(prices),
    }
    for entry in index.values():
        for key in ("tokens_in", "tokens_out", "cache_read", "cache_write"):
            total[key] += entry.get(key) or 0
        if entry.get("cost_usd") is None:
            total["unpriced"] += 1
        else:
            total["cost_usd"] = round((total["cost_usd"] or 0.0) + float(entry["cost_usd"]), 6)
            total["priced"] += 1
    return total
