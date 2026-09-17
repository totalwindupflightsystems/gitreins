"""Tests for per-judgment token/cost attribution (JVIEW-006)."""

import json
from pathlib import Path

from engine import usage


def _write_config(repo: Path, body: str) -> None:
    (repo / ".gitreins").mkdir(parents=True, exist_ok=True)
    (repo / ".gitreins" / "config.yaml").write_text(body, encoding="utf-8")


def _write_usage(repo: Path, rows: list) -> None:
    (repo / ".gitreins").mkdir(parents=True, exist_ok=True)
    with (repo / ".gitreins" / "usage.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row if isinstance(row, str) else json.dumps(row))
            handle.write("\n")


def _row(ts: float, tokens_in: int, tokens_out: int, **extra) -> dict:
    row = {"ts": ts, "tokens_in": tokens_in, "tokens_out": tokens_out, "step": "ai_eval"}
    row.update(extra)
    return row


# ── reading ──────────────────────────────────────────────────────────────────


def test_missing_usage_file_is_an_empty_list(tmp_path):
    assert usage.load_usage_rows(str(tmp_path)) == []


def test_usage_rows_skip_malformed_lines_and_sort_by_timestamp(tmp_path):
    _write_usage(
        tmp_path,
        [
            _row(300.0, 30, 3),
            "not-json",
            {"tokens_in": 1},  # no timestamp => unusable
            _row(100.0, 10, 1),
            "",
        ],
    )

    rows = usage.load_usage_rows(str(tmp_path))

    assert [row["ts"] for row in rows] == [100.0, 300.0]
    assert rows[0]["tokens_in"] == 10


def test_price_config_defaults_to_unpriced_and_falls_back_to_the_default_model(tmp_path):
    _write_config(tmp_path, "defaults:\n  model: deepseek-v4-flash\n")

    prices = usage.load_price_config(str(tmp_path))

    assert prices == {
        "model": "deepseek-v4-flash",
        "price_per_1m_input": 0.0,
        "price_per_1m_output": 0.0,
    }
    assert usage.prices_configured(prices) is False
    assert usage.cost_usd(1_000_000, 1_000_000, prices) is None


def test_price_config_reads_the_usage_block_and_garbage_rates_become_zero(tmp_path):
    _write_config(
        tmp_path,
        "defaults:\n  model: other-model\n"
        "usage:\n  model: deepseek-v4-flash\n"
        "  price_per_1m_input: 0.28\n"
        "  price_per_1m_output: nonsense\n",
    )

    prices = usage.load_price_config(str(tmp_path))

    assert prices["model"] == "deepseek-v4-flash"
    assert prices["price_per_1m_input"] == 0.28
    assert prices["price_per_1m_output"] == 0.0
    assert usage.prices_configured(prices) is True


def test_cost_is_charged_on_input_and_output_tokens_only(tmp_path):
    prices = {"model": "m", "price_per_1m_input": 0.28, "price_per_1m_output": 0.42}

    # 1M in + 1M out; cache reads are already included in tokens_in.
    assert usage.cost_usd(1_000_000, 1_000_000, prices) == 0.7
    assert usage.cost_usd(0, 0, prices) == 0.0


# ── attribution ──────────────────────────────────────────────────────────────


def test_each_row_is_charged_to_the_earliest_verdict_at_or_after_it():
    stamps = [("2026-09-01", "aaaa1111", 100.0), ("2026-09-02", "bbbb2222", 200.0)]
    rows = [
        _row(90.0, 10, 1),
        _row(95.0, 5, 1, step="tier1"),
        _row(199.0, 1000, 20),
        _row(500.0, 7, 7),  # after every verdict => unattributed, never invented
    ]

    index = usage.attribute_rows(stamps, rows)

    assert set(index) == {"2026-09-01/aaaa1111", "2026-09-02/bbbb2222"}
    first = index["2026-09-01/aaaa1111"]
    assert (first["tokens_in"], first["tokens_out"]) == (15, 2)
    assert first["rows"] == 2
    assert first["steps"] == ["ai_eval", "tier1"]
    assert first["last_ts"] == 95.0
    assert index["2026-09-02/bbbb2222"]["tokens_in"] == 1000
    # A row is charged exactly once: the totals equal the input rows.
    assert sum(entry["tokens_in"] for entry in index.values()) == 1015


def test_verdicts_without_telemetry_get_no_entry():
    index = usage.attribute_rows([("2026-09-01", "aaaa1111", 100.0)], [])

    assert index == {}


def test_attribution_carries_prices_and_marks_unpriced_judgements():
    stamps = [("2026-09-01", "aaaa1111", 100.0)]
    rows = [_row(50.0, 1_000_000, 1_000_000, cache_read=250)]

    unpriced = usage.attribute_rows(stamps, rows)
    priced = usage.attribute_rows(
        stamps, rows, {"model": "m", "price_per_1m_input": 0.28, "price_per_1m_output": 0.42}
    )

    assert unpriced["2026-09-01/aaaa1111"]["priced"] is False
    assert unpriced["2026-09-01/aaaa1111"]["cost_usd"] is None
    assert unpriced["2026-09-01/aaaa1111"]["cache_read"] == 250
    assert priced["2026-09-01/aaaa1111"]["priced"] is True
    assert priced["2026-09-01/aaaa1111"]["cost_usd"] == 0.7


def test_summarize_reports_a_priced_subtotal_next_to_the_unpriced_count():
    stamps = [
        ("2026-09-01", "aaaa1111", 100.0),
        ("2026-09-02", "bbbb2222", 200.0),
        ("2026-09-03", "cccc3333", 300.0),
    ]
    rows = [_row(50.0, 1_000_000, 0), _row(150.0, 1_000_000, 0)]
    prices = {"model": "m", "price_per_1m_input": 0.28, "price_per_1m_output": 0.0}
    index = usage.attribute_rows(stamps, rows, prices)

    summary = usage.summarize(index, prices, total_verdicts=len(stamps))

    assert summary["judgements"] == 2
    assert summary["verdicts"] == 3
    assert summary["unattributed"] == 1
    assert summary["priced"] == 2
    assert summary["unpriced"] == 0
    assert summary["tokens_in"] == 2_000_000
    assert summary["cost_usd"] == 0.56
    assert summary["model"] == "m"
    assert summary["prices_configured"] is True


def test_summarize_counts_unpriced_judgements_that_were_attributed():
    stamps = [("2026-09-01", "aaaa1111", 100.0)]
    rows = [_row(50.0, 10, 2)]
    index = usage.attribute_rows(stamps, rows)

    summary = usage.summarize(index, total_verdicts=2)

    assert (summary["priced"], summary["unpriced"], summary["unattributed"]) == (0, 1, 1)
    assert summary["cost_usd"] == 0.0
    assert summary["prices_configured"] is False


def test_summarize_of_an_empty_index_is_zero_and_unpriced_aware():
    summary = usage.summarize(
        {}, {"model": "", "price_per_1m_input": 0.0, "price_per_1m_output": 0.0}
    )

    assert summary["judgements"] == 0
    assert summary["verdicts"] == 0
    assert summary["unattributed"] == 0
    assert summary["cost_usd"] == 0.0
    assert summary["priced"] == 0
    assert summary["unpriced"] == 0
    assert summary["prices_configured"] is False
