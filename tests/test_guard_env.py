"""INT-FLAKE-8: the go guard lanes hand the `go` child a bounded build budget.

The go_tests stage compiles inside whatever process called the guard — a
pytest xdist worker, an agent, a judge. Under memory pressure a host whose
earlyoom runs with ``--prefer (^|/)(go|pytest|...)$`` kills exactly those
processes first, so an unbounded compile inflates the caller's envelope until
the OOM policy fires. ``check_go_tests``/``check_go_build`` therefore pass
GOMAXPROCS and ``GOFLAGS=-p=2`` defaults to the child and NEVER override an
explicit ambient value: the guard runs as a child of an agent/test process —
user intent wins, and a GOFLAGS already carrying ``-p`` is never doubled.

Hermetic: no go toolchain needed. The wiring tests stub
``engine.command_hygiene.run_bounded`` (the stub shape
tests/test_guard_manager.py already uses) and assert on the ``env`` kwarg the
lane hands it; the pass-path assertion keeps the lane's PASS contract intact
through the new env.
"""

import os

import engine.command_hygiene
from engine.guards import _go_build_env, check_go_build, check_go_tests


def _stub_lane_result(returncode: int = 0, output: str = "ok\n") -> dict:
    """A command_hygiene.run_bounded result (same contract the manager tests stub)."""
    return {
        "cmd": "stubbed",
        "exit_code": returncode,
        "output": output,
        "timed_out": False,
        "pgid": 424242,
    }


class TestGoBuildEnvDefaults:
    """_go_build_env bounds the build budget only where the caller was silent."""

    def test_defaults_added_when_unset(self) -> None:
        base = {"PATH": "/usr/bin"}
        env = _go_build_env(base)
        assert env["GOMAXPROCS"] == str(min(4, os.cpu_count() or 1))
        assert env["GOFLAGS"] == "-p=2"
        assert env["PATH"] == "/usr/bin"

    def test_base_dict_not_mutated(self) -> None:
        base = {"PATH": "/usr/bin"}
        _go_build_env(base)
        assert base == {"PATH": "/usr/bin"}

    def test_ambient_values_preserved_verbatim(self) -> None:
        env = _go_build_env({"GOMAXPROCS": "7", "GOFLAGS": "-p=5"})
        assert env["GOMAXPROCS"] == "7"
        assert env["GOFLAGS"] == "-p=5"

    def test_goflags_with_p_flag_not_doubled(self) -> None:
        env = _go_build_env({"GOFLAGS": "-p=5 -v"})
        assert env["GOFLAGS"] == "-p=5 -v"
        assert env["GOFLAGS"].count("-p=") == 1


class TestGoLaneEnvWiring:
    """The lanes hand the bounded env to run_bounded — not just build it."""

    def _capture_env(self, monkeypatch, tmp_path, ambient: dict[str, str]) -> dict:
        for name, value in ambient.items():
            monkeypatch.setenv(name, value)
        for name in ("GOMAXPROCS", "GOFLAGS"):
            if name not in ambient:
                monkeypatch.delenv(name, raising=False)
        (tmp_path / "x.go").write_text("package main\n", encoding="utf-8")
        captured: dict = {}

        def fake_run_bounded(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return _stub_lane_result()

        monkeypatch.setattr(engine.command_hygiene, "run_bounded", fake_run_bounded)
        return captured

    def test_check_go_tests_passes_bounded_env(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_env(monkeypatch, tmp_path, {})
        result = check_go_tests(str(tmp_path), changed_files=["x.go"])
        assert result.passed  # the lane's PASS contract survives the bounded env
        assert captured["env"]["GOMAXPROCS"] == str(min(4, os.cpu_count() or 1))
        assert captured["env"]["GOFLAGS"] == "-p=2"

    def test_check_go_tests_preserves_ambient_values(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_env(monkeypatch, tmp_path, {"GOMAXPROCS": "7", "GOFLAGS": "-p=5"})
        result = check_go_tests(str(tmp_path), changed_files=["x.go"])
        assert result.passed
        assert captured["env"]["GOMAXPROCS"] == "7"
        assert captured["env"]["GOFLAGS"] == "-p=5"

    def test_check_go_build_passes_bounded_env(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_env(monkeypatch, tmp_path, {})
        result = check_go_build(str(tmp_path), changed_files=["x.go"])
        assert result.passed
        assert captured["env"]["GOMAXPROCS"] == str(min(4, os.cpu_count() or 1))
        assert captured["env"]["GOFLAGS"] == "-p=2"

    def test_check_go_build_preserves_ambient_goflags(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_env(monkeypatch, tmp_path, {"GOFLAGS": "-mod=mod"})
        result = check_go_build(str(tmp_path), changed_files=["x.go"])
        assert result.passed
        assert captured["env"]["GOFLAGS"] == "-mod=mod"
