"""GR-154 Auger spec-alignment adapter and guard integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from engine.guard_manager import GuardManager
from engine.spec_alignment import run_checker


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "specs").mkdir()
    (repo / "specs" / "contract.md").write_text("contract\n")
    (repo / "source.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _checker(tmp_path: Path, body: str) -> str:
    path = tmp_path / "checker.py"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return str(path)


def _config(checker: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "command": checker,
        "spec_root": "specs",
        "mode": "warn",
        "missing_checker": "warn",
        "timeout": 2,
        "protocol_version": "1",
        "min_checker_version": "1.0.0",
    }
    value.update(overrides)
    return value


def test_disabled_default_is_compatibility_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("disabled gate invoked checker")

    monkeypatch.setattr("engine.spec_alignment.run_checker", forbidden)
    manager = GuardManager(
        str(repo),
        config={"guards": {"secrets": False, "lint": False, "tests": False}},
    )
    result = manager.run_all()
    assert called is False
    assert [guard.name for guard in result.results] == []
    assert result.passed is True


def test_enabled_pass_warn_and_confirmed_block(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for status, mode, passed, marker in (
        ("pass", "block", True, "output"),
        ("warn", "block", True, "warning"),
        ("warn", "warn", True, "warning"),
        ("fail", "block", False, "error"),
        ("fail", "warn", True, "warning"),
        ("block", "block", False, "error"),
    ):
        checker = _checker(
            tmp_path,
            "import json\n"
            f"print(json.dumps({{'protocol': '1', 'version': '1.0.0', 'status': '{status}', 'message': 'finding'}}))\n",
        )
        cfg = _config(checker, mode=mode)
        result = GuardManager(
            str(repo),
            config={
                "guards": {"secrets": False, "lint": False, "tests": False},
                "spec_alignment": cfg,
            },
        ).run_all()
        lane = next(guard for guard in result.results if guard.name == "spec_alignment")
        assert lane.passed is passed
        assert getattr(lane, marker)


def test_missing_checker_policy_and_handshake_are_fail_closed_when_configured(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    base = {"guards": {"secrets": False, "lint": False, "tests": False}}
    warn = GuardManager(
        str(repo),
        config={**base, "spec_alignment": _config("missing-auger", missing_checker="warn")},
    ).run_all()
    lane = next(guard for guard in warn.results if guard.name == "spec_alignment")
    assert lane.passed and lane.skipped and "checker missing" in lane.skip_reason

    block = GuardManager(
        str(repo),
        config={**base, "spec_alignment": _config("missing-auger", missing_checker="block")},
    ).run_all()
    lane = next(guard for guard in block.results if guard.name == "spec_alignment")
    assert not lane.passed and "checker missing" in lane.error

    mismatch = _checker(
        tmp_path,
        "import json\nprint(json.dumps({'protocol': '999', 'version': '1.0.0', 'status': 'pass'}))\n",
    )
    fail_closed = run_checker(str(repo), command=mismatch, mode="block", missing_checker="block")
    assert not fail_closed.passed
    assert "incompatible checker protocol" in fail_closed.error

    warn_default = run_checker(str(repo), command=mismatch, mode="block")
    assert warn_default.passed
    assert "incompatible checker protocol" in warn_default.warning

    for checker_body, expected_detail in (
        ("print('not-json')\nraise SystemExit(7)\n", "incompatible checker protocol"),
        (
            "import json\nprint(json.dumps({'protocol':'1','version':'1.0.0','status':'unknown'}))\n",
            "invalid status",
        ),
    ):
        invalid = _checker(tmp_path, checker_body)
        blocked = run_checker(str(repo), command=invalid, missing_checker="block")
        assert not blocked.passed
        assert expected_detail in blocked.error
        warned = run_checker(str(repo), command=invalid)
        assert warned.passed
        assert expected_detail in warned.warning


def test_staged_diff_and_repo_spec_arguments_are_scoped_without_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "source.txt").write_text("staged secret-like marker\n")
    _git(repo, "add", "source.txt")
    (repo / "source.txt").write_text("unstaged marker must not be sent\n")
    trace = tmp_path / "trace.json"
    checker = _checker(
        tmp_path,
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "payload = {'args': args, 'diff': sys.stdin.read()}\n"
        f"pathlib.Path({str(trace)!r}).write_text(json.dumps(payload))\n"
        "print(json.dumps({'protocol':'1','version':'1.0.0','status':'pass'}))\n",
    )
    before = (repo / "source.txt").read_bytes()
    result = run_checker(str(repo), command=checker, spec_root="specs")
    assert result.passed
    payload = json.loads(trace.read_text())
    args = payload["args"]
    assert args[args.index("--repo-root") + 1] == str(repo.resolve())
    assert args[args.index("--spec-root") + 1] == str((repo / "specs").resolve())
    assert "staged secret-like marker" in payload["diff"]
    assert "unstaged marker must not be sent" not in payload["diff"]
    assert (repo / "source.txt").read_bytes() == before
    assert _git_status(repo) == "MM source.txt"


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_timeout_joins_checker_process_group(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    child_pid = tmp_path / "child.pid"
    checker = _checker(
        tmp_path,
        "import os, pathlib, subprocess, time\n"
        f"child = subprocess.Popen(['sleep', '30'])\npathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
    )
    for policy, passed, marker in (
        ("block", False, "error"),
        ("warn", True, "warning"),
    ):
        child_pid.unlink(missing_ok=True)
        result = run_checker(
            str(repo),
            command=checker,
            mode="block",
            missing_checker=policy,
            timeout=0.1,
        )
        assert result.passed is passed
        assert result.timed_out
        assert "timed out" in getattr(result, marker)
        pid = int(child_pid.read_text())
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"checker child {pid} survived timeout")


def test_secret_bearing_checker_output_is_redacted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    checker = _checker(
        tmp_path,
        "import json\n"
        "secret = 'sk-' + 'live-1234567890'\n"
        "print('token=' + secret)\n"
        "print(json.dumps({'protocol':'1','version':'1.0.0','status':'warn'}))\n",
    )
    result = run_checker(str(repo), command=checker, mode="warn")
    assert result.passed
    assert "sk-live" not in result.output
    assert "[REDACTED]" in result.output


def test_endpoint_contract_is_json_and_uses_same_roots_and_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    seen: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"protocol":"1","version":"1.0.0","status":"pass"}'

    def fake_urlopen(request: object, timeout: float) -> Response:
        assert timeout == 2
        seen["request"] = request
        return Response()

    monkeypatch.setattr("engine.spec_alignment.urllib.request.urlopen", fake_urlopen)
    result = run_checker(str(repo), endpoint="https://auger.invalid/check", timeout=2)
    assert result.passed
    request = seen["request"]
    payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
    assert payload["repo_root"] == str(repo.resolve())
    assert payload["spec_root"] == str((repo / "specs").resolve())
    assert isinstance(payload["diff"], str)
