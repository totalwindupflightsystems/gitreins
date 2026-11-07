#!/usr/bin/env python3
"""GR-171 dry run: the second-pass reviewer over the last N landed commits.

Runs the author-independent second pass (engine.review_profiles.SecondPassReviewer)
over each of the last --n commits' diffs and writes a markdown report with, per
commit: the structured findings and a HUMAN-LABELING column for finding
precision. AC4 of GR-171 measures precision "as judged by a human sample" —
the machine's job here is only to produce the labeled sample honestly; the
precision number itself is filled in by a person reading each finding against
the commit.

Modes:
  * live  — uses the ambient LLMClient credential (GITREINS_LLM_* env).
  * mock  — GITREINS_MOCK_LLM_RESPONSE drives the reviewer, proving the
    plumbing only. Mock rows are labeled as such in the report; they are NOT
    evidence of review quality.

Usage:
  python scripts/review_dry_run.py --repo /path/to/repo --n 20 --out report.md
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _run_git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=15, cwd=repo, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def _commits(repo: str, n: int) -> list[dict[str, str]]:
    out = _run_git(repo, "log", f"-{n}", "--format=%H%x00%ae%x00%an%x00%s")
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, email, name, subject = line.split("\x00", 3)
        rows.append({"sha": sha, "email": email, "name": name, "subject": subject})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="repository whose commits are reviewed")
    parser.add_argument("--n", type=int, default=20, help="number of landed commits to review")
    parser.add_argument("--out", required=True, help="markdown report path to write")
    parser.add_argument(
        "--max-findings", type=int, default=10, help="second-pass findings cap per commit"
    )
    args = parser.parse_args()

    from engine.llm import LLMClient
    from engine.review_profiles import SecondPassReviewer, blocking_second_pass_findings

    mocked = bool(os.getenv("GITREINS_MOCK_LLM_RESPONSE"))
    llm = LLMClient()
    reviewer = SecondPassReviewer(llm, args.repo, max_findings=args.max_findings)

    commits = _commits(args.repo, args.n)
    lines: list[str] = [
        f"# GR-171 second-pass dry run — {len(commits)} commits, repo `{args.repo}`",
        "",
        f"Mode: {'MOCK (plumbing only — not evidence of review quality)' if mocked else 'LIVE'}",
        "",
        "Precision column: a human labels each finding TP (real defect the diff shows),",
        "FP (speculative/wrong) or N/A (unjudgeable from the diff alone). The precision",
        "figure is the human's count, never this script's.",
        "",
        "| commit | subject | author | findings (blocking) | precision (human) |",
        "|---|---|---|---|---|",
    ]
    detail_lines: list[str] = []
    for index, commit in enumerate(commits, 1):
        diff = _run_git(args.repo, "show", "--format=", commit["sha"])
        result = reviewer.review(diff_text=diff)
        blocking = blocking_second_pass_findings(result)
        lines.append(
            f"| {commit['sha'][:10]} | {commit['subject'][:60]} | {commit['email']} "
            f"| {len(result.findings)} ({len(blocking)} blocking) | _label TP/FP/N-A_ |"
        )
        detail_lines.append(
            f"\n## {index}. {commit['sha'][:10]} — {commit['subject']}\n"
            f"author: {commit['name']} <{commit['email']}>\n"
            f"reviewer: {result.reviewer_email or '(unknown)'} "
            f"self_review={result.self_review}\n"
        )
        if result.error:
            detail_lines.append(f"ERROR: {result.error}\n")
        for finding in result.findings:
            mark = "BLOCKING" if finding.severity in ("critical", "high") else "advisory"
            detail_lines.append(
                f"- [{finding.severity}/{mark}] {finding.file}:{finding.line} "
                f"detection: {finding.detection}\n"
                f"  detail: {finding.detail or '(none)'}\n  precision: _TP / FP / N-A_\n"
            )
        if not result.findings and not result.error:
            detail_lines.append("- (no findings)\n")

    report = "\n".join(lines) + "\n" + "\n".join(detail_lines) + "\n"
    out_path = Path(args.out)
    out_path.write_text(report, encoding="utf-8")
    print(f"wrote {out_path} ({len(commits)} commits, mode={'mock' if mocked else 'live'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
