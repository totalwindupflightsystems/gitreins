"""DF-GITREINS-POC-99: bunker-qa.sh launch --evidence writes to the caller's path.

The bug: launch() only parsed --keep, ignoring --evidence. When a caller passed
--evidence /custom/path.jsonl, launch wrote to the global default $EVIDENCE path
(/tmp/bunker-qa-evidence.jsonl), but collect() looked for /custom/path.jsonl.meta
which didn't exist. The caller's evidence file stayed empty (only launch rows),
while a SECOND evidence file at the harness default carried the actual cell rows.

Fix: launch() now parses --evidence and overrides the global $EVIDENCE before
writing anything.
"""

import subprocess
import tempfile
from pathlib import Path


def test_launch_parses_evidence_flag():
    """launch() must honor --evidence <path> and write to the caller's file."""
    script = Path.home() / ".hermes/scripts/bunker-qa.sh"
    assert script.exists(), f"bunker-qa.sh not found at {script}"

    with tempfile.TemporaryDirectory() as tmpdir:
        custom_evidence = Path(tmpdir) / "my-custom-evidence.jsonl"
        default_evidence = Path("/tmp/bunker-qa-evidence.jsonl")

        # Clean up any stale default evidence from prior runs
        if default_evidence.exists():
            default_evidence.unlink()

        # launch() will fail at preflights (no repo, no server), but it must
        # write the early meta record to the CUSTOM path, not the default.
        # We pass a non-existent repo to trigger an early exit.
        result = subprocess.run(
            [
                "bash",
                str(script),
                "launch",
                "/nonexistent/repo",
                "--evidence",
                str(custom_evidence),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # The script should have created the custom evidence file (or its .meta)
        # before failing. Check that the custom path was touched, not the default.
        meta_path = Path(f"{custom_evidence}.meta")
        default_meta = Path(f"{default_evidence}.meta")

        # Either the custom evidence or its meta must exist (launch writes early)
        custom_touched = custom_evidence.exists() or meta_path.exists()
        default_touched = default_evidence.exists() or default_meta.exists()

        assert custom_touched, (
            f"launch --evidence {custom_evidence} did not create the custom path "
            f"or its .meta. stderr: {result.stderr[:500]}"
        )
        assert not default_touched, (
            f"launch --evidence {custom_evidence} also wrote to the DEFAULT path "
            f"{default_evidence} — the fix did not override the global. "
            f"stderr: {result.stderr[:500]}"
        )


def test_launch_parses_evidence_equals_form():
    """launch() must honor --evidence=<path> (equals form)."""
    script = Path.home() / ".hermes/scripts/bunker-qa.sh"

    with tempfile.TemporaryDirectory() as tmpdir:
        custom_evidence = Path(tmpdir) / "equals-form.jsonl"
        default_evidence = Path("/tmp/bunker-qa-evidence.jsonl")

        if default_evidence.exists():
            default_evidence.unlink()

        result = subprocess.run(
            ["bash", str(script), "launch", "/nonexistent/repo", f"--evidence={custom_evidence}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        meta_path = Path(f"{custom_evidence}.meta")
        default_meta = Path(f"{default_evidence}.meta")

        custom_touched = custom_evidence.exists() or meta_path.exists()
        default_touched = default_evidence.exists() or default_meta.exists()

        assert custom_touched, (
            f"launch --evidence={custom_evidence} did not create the custom path. "
            f"stderr: {result.stderr[:500]}"
        )
        assert not default_touched, (
            f"launch --evidence={custom_evidence} also wrote to the DEFAULT path."
        )


def test_collect_reads_custom_evidence_meta():
    """collect() must find the .meta at the caller's --evidence path."""
    script = Path.home() / ".hermes/scripts/bunker-qa.sh"

    with tempfile.TemporaryDirectory() as tmpdir:
        custom_evidence = Path(tmpdir) / "collect-test.jsonl"
        meta_path = Path(f"{custom_evidence}.meta")

        # Simulate a launch that wrote to the custom path
        meta_path.write_text(
            "agent=test123\n"
            "server=test-server\n"
            f"evidence={custom_evidence}\n"
            "launched=2026-10-23T00:00:00Z\n"
            "keep=0\n"
            "project=test\n"
        )

        # collect should NOT say "no launch record" — the meta exists
        result = subprocess.run(
            ["bash", str(script), "collect", "--evidence", str(custom_evidence)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # It will fail to reach the agent (test123 doesn't exist), but it must
        # NOT fail with "no launch record" — that would mean it looked at the
        # wrong path.
        assert "no-launch-record" not in result.stdout, (
            f"collect --evidence {custom_evidence} said 'no launch record' even "
            f"though {meta_path} exists. stdout: {result.stdout}"
        )
