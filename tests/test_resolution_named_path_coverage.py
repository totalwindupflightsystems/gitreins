"""GR-226: named repository paths must be present in shipped resolution evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

from engine import resolution

QUESTION = "Does engine/target.py implement the requested behavior?"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = repo / "engine" / "target.py"
    target.parent.mkdir()
    target.write_text("def target():\n    return True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "engine/target.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=GR-226",
            "-c",
            "user.email=gr226@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return repo


def _verdict(monkeypatch, repo: Path, indexed: bool) -> resolution.ResolutionVerdict:
    manifest = []
    if indexed:
        manifest = [
            resolution.ManifestEntry(
                file="engine/target.py",
                provenance="ast_exact",
                score=1.0,
                bytes=24,
                truncated=False,
                source="understand",
                lines=2,
            )
        ]
    monkeypatch.setattr(
        resolution,
        "assemble_bundle",
        lambda *args, **kwargs: resolution.AssembledBundle(
            text="engine/target.py evidence", manifest=manifest
        ),
    )
    typed = {
        "resolves": resolution.TypedAnswer("resolves", "noul", 0.98),
        "missing_kind": resolution.TypedAnswer("missing_kind", "choice", "implementation"),
        "evidence_quality": resolution.TypedAnswer(
            "evidence_quality",
            "score",
            2.8,
            legend={"0": "topic", "1": "adjacent", "2": "exact", "3": "exact+test"},
        ),
    }
    monkeypatch.setattr(
        resolution,
        "call_jev",
        lambda *args, **kwargs: resolution.JevCallResult(payload={"_typed": typed, "usage": {}}),
    )
    return resolution.resolve(
        QUESTION, workdir=str(repo), bootstrap_index=False, traced=False, read_files=False
    )


def test_unindexed_named_tracked_path_is_marked_capped_and_not_called_missing_implementation(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    verdict = _verdict(monkeypatch, repo, indexed=False)

    record = verdict.to_dict()
    assert record["named_paths_missing"] == ["engine/target.py"]
    assert record["evidence_quality"] <= 1
    assert record["missing_kind"] != "implementation"
    assert record["verdict"] == "ABSTAIN"
    assert "engine/target.py" in (record["abstain_detail"] or "")


def test_indexed_named_tracked_path_has_no_marker_and_quality_is_not_capped(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    verdict = _verdict(monkeypatch, repo, indexed=True)

    record = verdict.to_dict()
    assert record["named_paths_missing"] == []
    assert record["evidence_quality"] == 3
    assert record["verdict"] != "ABSTAIN"
