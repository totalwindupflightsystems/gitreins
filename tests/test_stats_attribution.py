"""GR-229: /api/stats attribution asserts against the REAL persisted verdict schema.

engine/persist.py build_resolution_record nests the engine's own fields under
record["verdict"] (ResolutionVerdict.to_dict), so a record's ``model`` and
``cost_usd`` live THERE on disk. These tests drive the live HTTP server with
that shape and assert the aggregate echoes the record's own measurements —
with a config default present that must NOT win when the record names its
model — plus the fallback case (record without a model -> config default,
named via ``model_source``).
"""

import http.client
import json
import subprocess
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from gitreins import serve

# Deliberately DIFFERENT from the config default below: if the aggregate ever
# serves the default instead of the record, the assertion names it.
RECORD_MODEL = "typesafe/jev-1.13-20260917"
CONFIG_MODEL = "deepseek-v4-flash"
CONFIG_YAML = f"defaults:\n  model: {CONFIG_MODEL}\n"


def _nested_resolution_record(model: str | None, cost_usd: float | None) -> dict:
    """The REAL on-disk shape: usage truth lives under record["verdict"]."""
    nested: dict = {
        "input_tokens": 3000,
        "output_tokens": 500,
    }
    if model is not None:
        nested["model"] = model
    if cost_usd is not None:
        nested["cost_usd"] = cost_usd
    return {
        "kind": "resolution",
        "task_title": "GR-229 attribution fixture",
        "evaluated_at": "2026-09-17T12:00:00Z",
        "verdict": nested,
    }


@contextmanager
def running_server(workdir: str) -> Iterator[tuple[str, int]]:
    """Run the production request handler against one workdir only."""

    class FixtureHandler(serve.Handler):
        pass

    FixtureHandler.workdir = workdir
    FixtureHandler.project = ""
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "judgment viewer server thread did not stop"


def _get(address: tuple[str, int], path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def _fixture_repo(tmp_path: Path, record: dict) -> Path:
    verdict_dir = tmp_path / ".gitreins" / "history" / "2026-09-17" / "b1a2c3d4"
    verdict_dir.mkdir(parents=True)
    (verdict_dir / "verdict.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / ".gitreins" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    return tmp_path


def test_stats_echoes_the_record_model_and_cost_from_the_nested_verdict(
    tmp_path: Path,
) -> None:
    record = _nested_resolution_record(model=RECORD_MODEL, cost_usd=0.012345)
    _fixture_repo(tmp_path, record)

    with running_server(str(tmp_path)) as address:
        status, payload = _get(address, "/api/stats")

    assert status == 200
    usage = payload["usage"]
    assert usage["judgements"] == 1
    # The record's own model wins over the (deliberately different) config
    # default, and the payload names the source.
    assert usage["model"] == RECORD_MODEL
    assert usage["model_source"] == "record"
    # The record's provider-reported cost is the aggregate cost, not an
    # estimate from the configured rates.
    assert usage["cost_usd"] == 0.012345
    assert usage["priced"] == 1
    assert usage["unpriced"] == 0


def test_stats_falls_back_to_the_config_default_when_the_record_has_no_model(
    tmp_path: Path,
) -> None:
    # Same nested persisted shape, but the engine verdict carries no model.
    record = _nested_resolution_record(model=None, cost_usd=0.5)
    _fixture_repo(tmp_path, record)

    with running_server(str(tmp_path)) as address:
        status, payload = _get(address, "/api/stats")

    assert status == 200
    usage = payload["usage"]
    assert usage["judgements"] == 1
    assert usage["model"] == CONFIG_MODEL
    assert usage["model_source"] == "config-default"
    # A provider-reported cost is kept even without a record model.
    assert usage["cost_usd"] == 0.5
    assert usage["priced"] == 1
    assert usage["unpriced"] == 0
