"""GR-165: `gitreins doctor` answers wiring reachability, not just call counts.

`engine/dead_code.py` counts call sites and whitelists exactly the dispatcher
names (main/run/handle/process/execute/dispatch) where wiring bugs live. The
wiring check asks the question the owner actually has: is everything defined
reachable from an entry point, and is every handler/route/tool registered
somewhere that is itself reachable? These fixtures prove the four unwired
shapes, the exemption reporting, the consumed-registry negative control, and
that gitreins' own tree stays clean.
"""

from __future__ import annotations

import os
import subprocess
import sys

from engine import doctor, install_manifest, wiring

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_BODY = "#!/bin/sh\n# gitreins-managed — test hook\nexit 0\n"


def _write(workdir: str, rel: str, text: str) -> None:
    path = os.path.join(workdir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _repo(tmp_path, files: dict[str, str]) -> str:
    """A plain temp repo (no pyproject — the fallback sweep covers every .py)."""
    workdir = str(tmp_path / "repo")
    os.makedirs(workdir, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    for rel, text in files.items():
        _write(workdir, rel, text)
    return workdir


def _installed(workdir: str) -> None:
    """A minimal healthy install so `doctor` runs the health checks."""
    artifacts = []
    for name in install_manifest.HOOK_NAMES:
        path = install_manifest._hook_path(workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(HOOK_BODY)
        os.chmod(path, 0o755)
        artifacts.append(
            install_manifest.Artifact(
                os.path.relpath(path, workdir), "hook", install_manifest.sha256_file(path)
            )
        )
    install_manifest.record_install(workdir, artifacts)


def _findings(report: wiring.WiringReport, suffix: str) -> list[wiring.WiringFinding]:
    return [f for f in report.findings if f.symbol.split(":")[-1].endswith(suffix)]


# ── Shape (a): CLI subcommand implemented but missing from the dispatch table


def test_cli_subcommand_missing_from_dispatch_is_flagged(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/cli.py": (
                'HANDLERS = {"greet": "cmd_greet"}\n'
                "\n"
                "def cmd_greet():\n"
                '    return "hi"\n'
                "\n"
                "def cmd_bye():\n"
                '    return "bye"\n'
                "\n"
                "def main():\n"
                "    return cmd_greet()\n"
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    hits = _findings(report, "cmd_bye")
    assert len(hits) == 1, [f.render() for f in report.findings]
    hit = hits[0]
    assert hit.file == "app/cli.py"
    assert hit.line > 1
    assert "not reachable" in hit.reason
    # The wired command is NOT flagged.
    assert not _findings(report, "cmd_greet")


# ── Shape (b): handler/router constructed but never mounted/registered


def test_handler_constructed_but_never_mounted_is_flagged(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/service.py": (
                "class Router:\n"
                "    def handle(self, request):\n"
                "        return request\n"
                "\n"
                "def build_router():\n"
                "    router = Router()\n"
                "    return router\n"
                "\n"
                "def main():\n"
                '    return "up"\n'
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    router_hits = _findings(report, "Router")
    assert len(router_hits) == 1, [f.render() for f in report.findings]
    # The evidence names the unreachable construction site (line 6: the
    # `router = Router()` inside build_router).
    assert router_hits[0].reason == "referenced only from unreachable code: app/service.py:6"
    # The builder itself is unwired too; `handle` is a whitelisted dispatcher
    # name, so it must surface as an exemption, never as a silent pass.
    assert len(_findings(report, "build_router")) == 1
    assert any(e.name == "handle" for e in report.exemptions)


# ── Shape (c): evaluator tool implemented but absent from the tool registry


def test_evaluator_tool_absent_from_registry_and_dispatch_is_flagged(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/loop.py": (
                'EVALUATOR_TOOLS = ["read_file"]\n'
                "\n"
                "class Loop:\n"
                "    def _tool_read_file(self):\n"
                "        return {}\n"
                "\n"
                "    def _tool_greet(self):\n"
                "        return {}\n"
                "\n"
                "    def _execute_tool(self, name):\n"
                '        if name == "read_file":\n'
                "            return self._tool_read_file()\n"
                "        return {}\n"
                "\n"
                "def run():\n"
                "    return list(EVALUATOR_TOOLS)\n"
                "\n"
                "def main():\n"
                "    return run()\n"
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    greet_hits = _findings(report, "_tool_greet")
    assert len(greet_hits) == 1, [f.render() for f in report.findings]
    assert greet_hits[0].file == "app/loop.py"
    assert "not reachable" in greet_hits[0].reason


def test_evaluator_tool_dispatched_but_unadvertised_is_drift(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/loop2.py": (
                'TOOLS = {"read_file": "read a file"}\n'
                "\n"
                "class Loop:\n"
                "    def _tool_read_file(self):\n"
                "        return {}\n"
                "\n"
                "    def _tool_greet(self):\n"
                "        return {}\n"
                "\n"
                "    def _execute_tool(self, name):\n"
                '        if name == "read_file":\n'
                "            return self._tool_read_file()\n"
                '        if name == "greet":\n'
                "            return self._tool_greet()\n"
                "        return {}\n"
                "\n"
                "def main():\n"
                "    return sorted(TOOLS)\n"
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    greet_hits = _findings(report, "_tool_greet")
    assert len(greet_hits) == 1, [f.render() for f in report.findings]
    assert "dispatched but absent" in greet_hits[0].reason
    # The advertised tool is consistent — no drift finding for it.
    assert not [f for f in report.findings if "read_file" in f.reason]


# ── Shape (d): config key read only by a path that is never called


def test_config_key_read_only_by_uncalled_path_is_flagged(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/config.py": (
                'CONFIG = {"verbose_banner": True}\n'
                "\n"
                "def verbose_banner():\n"
                '    if CONFIG["verbose_banner"]:\n'
                '        return "banner"\n'
                '    return ""\n'
                "\n"
                "def main():\n"
                '    return "start"\n'
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    hits = _findings(report, "verbose_banner")
    assert len(hits) == 1, [f.render() for f in report.findings]
    assert "verbose_banner" in hits[0].reason  # the key is named in the reason


# ── Exemption reporting (never silent)


def test_whitelisted_dispatcher_name_is_reported_as_exemption(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/cli2.py": (
                'HANDLERS = {"hello": "cmd_hello"}\n'
                "\n"
                "def cmd_hello():\n"
                "    return 1\n"
                "\n"
                "def dispatch():\n"
                "    return 2\n"
                "\n"
                "def main():\n"
                "    return cmd_hello()\n"
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    assert report.findings == [], [f.render() for f in report.findings]
    assert len(report.exemptions) == 1
    exemption = report.exemptions[0]
    assert exemption.name == "dispatch"
    assert exemption.symbol.endswith("app.cli2:dispatch")
    assert exemption.source.startswith("dead_code.WHITELIST_FUNCTIONS")
    status, detail, _hint = wiring.summarize(report)
    assert status == "ok"
    assert "dispatch" in detail and "exemption" in detail


# ── Negative control: a correctly wired fixture yields no findings


def test_correctly_wired_fixture_yields_no_findings(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/ok.py": (
                'HANDLERS = {"greet": "cmd_greet", "bye": "cmd_bye"}\n'
                "\n"
                "def cmd_greet():\n"
                "    return helper()\n"
                "\n"
                "def cmd_bye():\n"
                "    return 0\n"
                "\n"
                "def helper():\n"
                "    return 42\n"
                "\n"
                "def main():\n"
                "    return [globals()[HANDLERS[name]]() for name in HANDLERS]\n"
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    assert report.findings == [], [f.render() for f in report.findings]
    assert report.exemptions == []


def test_chain_reachable_through_consumed_registry_stays_clean(tmp_path) -> None:
    """A chain wired ONLY through a consumed registry must not be flagged."""
    workdir = _repo(
        tmp_path,
        {
            "app/wired.py": (
                'TOOLS = {"fetch": "cmd_fetch"}\n'
                "\n"
                "def cmd_fetch():\n"
                "    return deep_helper()\n"
                "\n"
                "def deep_helper():\n"
                "    return 1\n"
                "\n"
                "def main():\n"
                '    return globals()[TOOLS["fetch"]]()\n'
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
        },
    )
    report = wiring.scan(workdir)
    assert report.findings == [], [f.render() for f in report.findings]
    # The chain IS wired: cmd_fetch and deep_helper surface in no finding —
    # reachability flows through the consumed registry (the orphan registry
    # that flags its handlers is the next test's arm).


def test_unconsumed_registry_literal_flags_its_handlers(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/orphan.py": (
                'ORPHAN = {"hide": "cmd_hide"}\n'
                "\n"
                "def cmd_hide():\n"
                "    return 2\n"
                "\n"
                "def main():\n"
                '    return "up"\n'
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    report = wiring.scan(workdir)
    hits = _findings(report, "cmd_hide")
    assert len(hits) == 1, [f.render() for f in report.findings]
    assert "unconsumed registry literal ORPHAN" in hits[0].reason
    assert "app/orphan.py:1" in hits[0].reason


# ── Zero false positives on gitreins' own tree (reported exemptions allowed)


def test_gitreins_tree_itself_has_zero_findings() -> None:
    report = wiring.scan(REPO_ROOT)
    assert report.modules_scanned > 50
    assert report.findings == [], [f.render() for f in report.findings]
    # Every exemption carries a reported source — never a silent suppression —
    # and the whitelist reuse is live (dispatcher names surface as exemptions).
    assert report.exemptions, "expected at least one reported exemption on this tree"
    for exemption in report.exemptions:
        assert exemption.source, exemption.render()


# ── Doctor integration: warn by default, fail under --strict, exit contract


def test_doctor_wiring_warns_and_strict_fails(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/service.py": (
                "class Router:\n"
                "    def handle(self, request):\n"
                "        return request\n"
                "\n"
                "def build_router():\n"
                "    router = Router()\n"
                "    return router\n"
                "\n"
                "def main():\n"
                '    return "up"\n'
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    _installed(workdir)
    checks = doctor.run_checks(workdir)
    wiring_check = next(check for check in checks if check.name == "wiring")
    assert wiring_check.status == "warn"
    assert "build_router" in wiring_check.detail
    assert not doctor.has_failures(checks)  # warn alone never changes the exit code

    strict = doctor.run_checks(workdir, strict=True)
    strict_check = next(check for check in strict if check.name == "wiring")
    assert strict_check.status == "fail"
    assert doctor.has_failures(strict)


def test_cli_doctor_strict_exit_contract(tmp_path) -> None:
    workdir = _repo(
        tmp_path,
        {
            "app/service.py": (
                "class Router:\n"
                "    def handle(self, request):\n"
                "        return request\n"
                "\n"
                "def build_router():\n"
                "    router = Router()\n"
                "    return router\n"
                "\n"
                "def main():\n"
                '    return "up"\n'
                "\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        },
    )
    _installed(workdir)
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    plain = subprocess.run(
        [sys.executable, "-m", "gitreins", "doctor"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "[WARN] wiring" in plain.stdout
    assert "app/service.py" in plain.stdout and "build_router" in plain.stdout

    strict = subprocess.run(
        [sys.executable, "-m", "gitreins", "doctor", "--strict"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert strict.returncode != 0
    assert "[FAIL] wiring" in strict.stdout
    # The finding names file:line, the symbol and the reason.
    assert "app/service.py" in strict.stdout
    assert "build_router" in strict.stdout
    assert "not reachable" in strict.stdout
