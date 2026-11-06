"""Staged security review pipeline (GR-156) — three stages over the change set.

The pipeline is driven by the top-level ``security_review:`` block of
.gitreins/config.yaml and surfaced by the guard's ``security_review`` lane
(engine.guard_manager.GuardManager). Three modes:

  * ``off``    — no stage runs; guard output is unchanged (AC1).
  * ``staged`` — stage 1 + stage 2 over the staged files.
  * ``full``   — stage 1 + stage 2 + the LLM judge (stage 3).

Stage order and contracts:

Stage 1 ``rules``    — deterministic and offline: semgrep and ast-grep run
    security rules over the STAGED FILES ONLY. No ML and no LLM here (AC2).
    Both tools are optional; neither is installed by GitReins. Every finding
    carries ``rule_id``, ``CWE``, ``file:line`` and ``stage="rules"``.

Stage 2 ``localize`` — the existing Antares scanner (engine.antares, GR-117)
    over the SAME file set. Findings carry ``stage="localize"`` and a
    confidence score; anything below ``min_confidence`` is DROPPED and the
    drop is counted — never silently promoted (AC3).

Stage 3 ``judge``    — full mode ONLY. Receives the stage-1/2 hypotheses at
    or above ``min_confidence``; every finding it confirms must cite
    ``file:line``; a hypothesis the judge cannot cite is reported as
    ``unconfirmed`` — never as a pass (AC4).

Bounded by construction (AC6): every stage runs under
``per_stage_timeout_seconds`` — stage 1 spawns its tools as subprocesses with
that timeout, stage 3 runs the judge callable in a daemon thread joined under
the same budget. Stages 1 and 2 never touch the network (rule tables are
rendered to temporary files, never fetched); stage 3 talks only to the
operator's configured LLM endpoint through engine.llm. No stage writes into
the repository, and no tool process outlives its stage.

Every reported finding names its producing stage (AC5): a rule hit is always
``[rules]`` and can never be labelled ``model-confirmed`` — that label exists
only for stage-3 confirmations.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("gitreins.security_stages")

# ── Stage vocabulary ─────────────────────────────────────────────────────────

STAGE_RULES = "rules"
STAGE_LOCALIZE = "localize"
STAGE_JUDGE = "judge"

# Stage lifecycle statuses. Everything except "ok" and "skipped" is a
# DEGRADED stage: it graded nothing, and SecurityReviewConfig.missing_tool_policy
# decides whether that is a lane failure ("fail") or a named skip ("skip").
# For the rules stage "ok" includes PARTIAL COVERAGE: at least one of its two
# optional tools ran, the other being absent on this host (named in
# StageOutcome.notes) — "missing_tool" means NO rules tool ran (GR-GAP-067).
STATUS_OK = "ok"
STATUS_MISSING_TOOL = "missing_tool"
STATUS_TIMEOUT = "timeout"
STATUS_TOOL_ERROR = "tool_error"
STATUS_SKIPPED = "skipped"

_DEGRADED_STATUSES = frozenset({STATUS_MISSING_TOOL, STATUS_TIMEOUT, STATUS_TOOL_ERROR})

#: Files above this size are not read by stage 2 (bounded execution).
_MAX_STAGE2_FILE_BYTES = 1_000_000

# A judge citation must be exactly "path:line" — the file:line form AC4
# requires. A path may not be empty; the line must be an integer.
_CITATION_RE = re.compile(r"^.+:\d+$")
_CWE_RE = re.compile(r"CWE-\d+")

# ── Types ────────────────────────────────────────────────────────────────────

#: Spawns one tool invocation. Returns (returncode, stdout, stderr). Raises
#: FileNotFoundError when the binary is absent and subprocess.TimeoutExpired
#: when the stage budget elapses. (workdir, argv, timeout_seconds)
ToolRunner = Callable[[str, list[str], float], tuple[int, str, str]]

#: Builds a scanner exposing ``scan_file(abspath) -> [findings]``.
ScannerFactory = Callable[[str], Any]

#: Judge callable: hypotheses in, verdict dicts out. Each verdict carries
#: ``rule_id``, ``confirmed`` (bool), ``citation`` ("path:line") and ``note``.
JudgeFn = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


@dataclass
class SecurityFinding:
    """One security finding, labelled with the stage that produced it (AC5)."""

    stage: str  # "rules" | "localize" | "judge"
    rule_id: str
    cwe: str
    file: str
    line: int
    confidence: float
    description: str = ""
    #: Stage 3 only: True when the judge confirmed AND cited the hypothesis.
    confirmed: bool | None = None

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    def stage_label(self) -> str:
        """The stage tag every rendered line carries — a rule hit is never
        relabelled by a later stage."""
        return f"[{self.stage}]"

    def verdict_label(self) -> str:
        """What produced this finding, in reviewer vocabulary."""
        if self.stage == STAGE_RULES:
            return "rule hit"
        if self.stage == STAGE_LOCALIZE:
            return "model-localized"
        if self.confirmed:
            return "model-confirmed"
        return "unconfirmed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "rule_id": self.rule_id,
            "cwe": self.cwe,
            "file": self.file,
            "line": self.line,
            "confidence": self.confidence,
            "description": self.description,
            "confirmed": self.confirmed,
        }


@dataclass
class StageOutcome:
    """One stage's result: status, findings, and the drop/skip accounting."""

    stage: str
    status: str
    findings: list[SecurityFinding] = field(default_factory=list)
    #: Stage 2: hypotheses dropped for being below min_confidence.
    dropped_below_floor: int = 0
    #: Stage 3: hypotheses handed to the judge.
    escalations: int = 0
    #: Stage 3: hypotheses the judge could not confirm-and-cite (AC4).
    unconfirmed: list[str] = field(default_factory=list)
    #: Human-readable notes: which tool was missing, why a stage skipped.
    detail: str = ""
    #: Per-tool availability notes for a stage with optional tools (rules):
    #: names a tool that is unavailable on this host while the stage still
    #: ran and graded — partial coverage, not absence (GR-GAP-067).
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.status in _DEGRADED_STATUSES


@dataclass
class StagedReviewOutcome:
    """The whole pipeline run over one change set."""

    mode: str
    findings: list[SecurityFinding] = field(default_factory=list)
    stages: list[StageOutcome] = field(default_factory=list)
    escalations: int = 0
    unconfirmed: list[str] = field(default_factory=list)
    passed: bool = True

    def failed_stages(self) -> list[StageOutcome]:
        return [s for s in self.stages if s.degraded]

    def skipped_stages(self) -> list[StageOutcome]:
        return [s for s in self.stages if s.status == STATUS_SKIPPED]

    def dropped_below_floor(self) -> int:
        return sum(s.dropped_below_floor for s in self.stages)


# ── Injectable seams (monkeypatch targets for hermetic tests) ───────────────


def _default_tool_runner(workdir: str, argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Spawn *argv* bounded: own process group exit, no orphans, GIT_* scrubbed.

    The GIT_* scrub mirrors engine.guard_manager._sanitized_env: a pre-commit
    hook exports GIT_INDEX_FILE and friends, and a spawned scanner must read
    the workdir's own repository state, not the hook caller's.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=workdir,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _default_scanner_factory(workdir: str) -> Any:
    from engine.antares import AntaresScanner

    return AntaresScanner(workdir)


_UNSET = object()


def build_judge_fn(config: Any) -> JudgeFn | None:
    """The default LLM judge, or None when no judge is reachable (AC7).

    Unavailable means: engine.llm cannot be imported, or no credential is
    configured on the resolved client. The caller then applies
    ``missing_tool_policy`` to stage 3 — a missing judge is never a silent
    pass.
    """
    try:
        from engine.llm import LLMClient

        client = LLMClient()
    except Exception as exc:  # noqa: BLE001 - any build failure = judge unavailable
        logger.debug("security review judge unavailable: %s", exc)
        return None
    if not getattr(client, "api_key", ""):
        return None

    def _judge(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = _render_judge_prompt(hypotheses)
        response = client.chat(
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
            temperature=0.0,
        )
        return _parse_judge_response(getattr(response, "content", ""))

    return _judge


# ── Stage 1: deterministic rules ─────────────────────────────────────────────

# Bundled, offline rule tables. They are rendered to TEMPORARY files so no
# rule file ever lands inside the graded repository (AC6), and so the tools
# never fetch anything from a rule registry (no network).
_SEMGREP_RULES_YAML = """rules:
  - id: gitreins-eval-call
    languages: [python]
    severity: WARNING
    message: "Use of eval() can execute attacker-controlled code"
    metadata:
      cwe: "CWE-95: Eval Injection"
    pattern: eval(...)
  - id: gitreins-exec-call
    languages: [python]
    severity: WARNING
    message: "Use of exec() can execute attacker-controlled code"
    metadata:
      cwe: "CWE-94: Improper Control of Generation of Code"
    pattern: exec(...)
  - id: gitreins-pickle-loads
    languages: [python]
    severity: WARNING
    message: "pickle.loads() on untrusted data can run arbitrary code"
    metadata:
      cwe: "CWE-502: Deserialization of Untrusted Data"
    pattern: pickle.loads(...)
  - id: gitreins-yaml-unsafe-load
    languages: [python]
    severity: WARNING
    message: "yaml.load() without SafeLoader deserializes untrusted objects"
    metadata:
      cwe: "CWE-502: Deserialization of Untrusted Data"
    pattern: yaml.load(...)
"""

_ASTGREP_RULES_YAML = """id: gitreins-os-system
language: python
rule:
  pattern: os.system($$$CMD)
severity: warning
message: "os.system() spawns a shell — command injection risk (CWE-78)"
"""

_SEMGREP = "semgrep"
_ASTGREP = "ast-grep"


def _tool_on_path(tool: str) -> str | None:
    """PATH probe for an optional stage-1 tool.

    A module-level seam so hermetic tests can hide or present a tool without
    touching the global ``shutil`` module other code shares.
    """
    return shutil.which(tool)


def run_rules_stage(
    workdir: str,
    files: list[str],
    timeout: float,
    tool_runner: ToolRunner | None = None,
) -> StageOutcome:
    """Stage 1: semgrep + ast-grep over *files* only. No ML, no LLM (AC2).

    The two tools are redundant optional scanners, so the stage is covered
    when AT LEAST ONE of them runs: a single absent tool is PARTIAL COVERAGE
    (status ``ok``, the absent tool named in ``detail`` and ``notes``), not a
    ``missing_tool`` stage. The stage degrades only when no rules tool ran at
    all (``missing_tool`` — governed by ``missing_tool_policy``) or when a
    tool that IS present times out or fails (``timeout`` / ``tool_error``),
    whether or not the other tool ran. A ``FileNotFoundError`` from the
    runner for a probed-present binary counts as absence, not as a stage
    failure, while the other tool covers the stage. Findings already
    collected before a degradation are kept.
    """
    runner = tool_runner or _default_tool_runner
    findings: list[SecurityFinding] = []
    notes: list[str] = []  # availability/tool notes, kept on the OK return too
    ok_summary: list[str] = []  # per-tool finding counts for the OK detail
    degraded_status = ""
    degraded_detail = ""
    ran_ok = False

    tools = (
        (
            _SEMGREP,
            [_SEMGREP, "--config", "%RULES%", "--json"],
            _parse_semgrep_output,
            "pip install semgrep",
        ),
        (
            _ASTGREP,
            [_ASTGREP, "scan", "--rule", "%RULES%", "--json=compact"],
            _parse_astgrep_output,
            "cargo install ast-grep",
        ),
    )
    for tool, argv_template, parser, install_hint in tools:
        if _tool_on_path(tool) is None:
            notes.append(f"{tool} unavailable ({install_hint})")
            continue
        rc, out, err, spawn_status = _spawn_tool(runner, workdir, argv_template, files, timeout)
        if spawn_status == STATUS_MISSING_TOOL:
            # FileNotFoundError between the PATH probe and the spawn: treat
            # as absent on this host, not as a stage failure (GR-GAP-067).
            notes.append(f"{tool} unavailable ({install_hint})")
            continue
        if spawn_status != STATUS_OK:
            notes.append(_spawn_detail(tool, spawn_status))
            if not degraded_status:
                degraded_status = spawn_status
                degraded_detail = _spawn_detail(tool, spawn_status)
            continue
        ran_ok = True
        parsed, parse_error = parser(out, rc, err)
        findings.extend(parsed)
        ok_summary.append(f"{tool}: {len(parsed)} finding(s)")
        if parse_error:
            notes.append(parse_error)
            if not degraded_status:
                degraded_status, degraded_detail = STATUS_TOOL_ERROR, parse_error
        elif rc != 0 and not parsed and not out.strip():
            # A non-zero exit with no parseable findings is a broken run, not
            # a clean scan — never report it as an ok stage.
            detail = f"{tool} exited {rc} with no findings output: {err.strip()[:200]}"
            notes.append(detail)
            if not degraded_status:
                degraded_status, degraded_detail = STATUS_TOOL_ERROR, detail

    if not ran_ok and not degraded_status:
        # No rules tool ran at all: a genuinely absent stage.
        degraded_status, degraded_detail = STATUS_MISSING_TOOL, "; ".join(notes)
    if degraded_status:
        return StageOutcome(
            stage=STAGE_RULES,
            status=degraded_status,
            findings=findings,
            detail=degraded_detail,
            notes=notes,
        )
    # Partial coverage still graded: name the absent tool, keep the counts.
    detail = "; ".join([*notes, *ok_summary]) if notes else ""
    return StageOutcome(
        stage=STAGE_RULES, status=STATUS_OK, findings=findings, detail=detail, notes=notes
    )


def _spawn_detail(tool: str, status: str) -> str:
    if status == STATUS_MISSING_TOOL:
        return f"{tool} not found"
    if status == STATUS_TIMEOUT:
        return f"{tool} exceeded the per-stage timeout"
    return f"{tool} failed"


def _spawn_tool(
    runner: ToolRunner,
    workdir: str,
    argv_template: list[str],
    files: list[str],
    timeout: float,
) -> tuple[int, str, str, str]:
    """Render the rule table into a temp file, spawn the tool over *files*.

    Returns (returncode, stdout, stderr, status); status is STATUS_OK or one
    of the degraded statuses. The staged file list is passed verbatim as the
    scan targets — the tool never widens its own scope (AC6). The temp rules
    file lives outside the graded repository and is removed on every path.
    """
    rules_yaml = (
        _SEMGREP_RULES_YAML
        if argv_template and argv_template[0] == _SEMGREP
        else _ASTGREP_RULES_YAML
    )
    suffix = ".semgrep.yml" if argv_template[0] == _SEMGREP else ".ast-grep.yml"
    fd, rules_path = tempfile.mkstemp(prefix="gitreins-security-rules-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rules_yaml)
        argv = [rules_path if part == "%RULES%" else part for part in argv_template]
        argv.extend(files)
        try:
            rc, out, err = runner(workdir, argv, timeout)
        except FileNotFoundError:
            return 1, "", "", STATUS_MISSING_TOOL
        except subprocess.TimeoutExpired:
            return 1, "", "", STATUS_TIMEOUT
        except OSError:
            return 1, "", "", STATUS_TOOL_ERROR
    finally:
        with contextlib.suppress(OSError):
            os.unlink(rules_path)
    return rc, out, err, STATUS_OK


def _cwe_of(text: str) -> str:
    match = _CWE_RE.search(text or "")
    return match.group(0) if match else "CWE-UNSPECIFIED"


def _parse_semgrep_output(
    stdout: str, returncode: int, stderr: str
) -> tuple[list[SecurityFinding], str]:
    """Semgrep JSON results -> findings; (empty, reason) when unparseable."""
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        detail = f"semgrep output was not JSON (exit {returncode}): {stderr.strip()[:200]}"
        return [], detail
    if not isinstance(data, dict):
        return [], f"semgrep output was not a JSON object (exit {returncode})"
    findings: list[SecurityFinding] = []
    for item in data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        start = item.get("start") or {}
        extra = item.get("extra") or {}
        metadata = extra.get("metadata") or {}
        cwe = metadata.get("cwe", "")
        if isinstance(cwe, list):
            cwe = cwe[0] if cwe else ""
        findings.append(
            SecurityFinding(
                stage=STAGE_RULES,
                rule_id=str(item.get("check_id", "semgrep")),
                cwe=_cwe_of(str(cwe)),
                file=str(item.get("path", "")),
                line=int(start.get("line", 0) or 0),
                confidence=1.0,  # deterministic rule match, not a model score
                description=str(extra.get("message", "")),
            )
        )
    return findings, ""


def _parse_astgrep_output(
    stdout: str, returncode: int, stderr: str
) -> tuple[list[SecurityFinding], str]:
    """ast-grep JSON results -> findings; (empty, reason) when unparseable."""
    try:
        data = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        detail = f"ast-grep output was not JSON (exit {returncode}): {stderr.strip()[:200]}"
        return [], detail
    if not isinstance(data, list):
        return [], f"ast-grep output was not a JSON array (exit {returncode})"
    findings: list[SecurityFinding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start = (item.get("range") or {}).get("start") or {}
        # ast-grep reports 0-based line numbers; findings are 1-based.
        line = int(start.get("line", -1))
        message = str(item.get("message", "") or "")
        text = str(item.get("text", "") or "")
        rules = item.get("rules") or [item.get("ruleId") or item.get("rule_id")]
        rule_id = str(rules[0]) if rules and rules[0] else "ast-grep"
        findings.append(
            SecurityFinding(
                stage=STAGE_RULES,
                rule_id=rule_id,
                cwe=_cwe_of(message) if _CWE_RE.search(message) else _cwe_of(text),
                file=str(item.get("file", "")),
                line=line + 1,
                confidence=1.0,  # deterministic rule match, not a model score
                description=message or text[:200],
            )
        )
    return findings, ""


# ── Stage 2: Antares localization ────────────────────────────────────────────


def run_localization_stage(
    workdir: str,
    files: list[str],
    min_confidence: float,
    timeout: float,
    scanner_factory: ScannerFactory | None = None,
) -> StageOutcome:
    """Stage 2: the existing Antares scanner over the SAME file set (AC3).

    Findings below *min_confidence* are dropped AND counted in
    ``dropped_below_floor`` — never silently promoted. A scanner that cannot
    be built (ImportError: the optional ML stack is absent) reports
    ``missing_tool``; the caller applies ``missing_tool_policy``.
    """
    factory = scanner_factory or _default_scanner_factory
    try:
        scanner = factory(workdir)
    except ImportError as exc:
        return StageOutcome(
            stage=STAGE_LOCALIZE,
            status=STATUS_MISSING_TOOL,
            detail=f"Antares scanner unavailable: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - a broken scanner is a tool error
        return StageOutcome(
            stage=STAGE_LOCALIZE,
            status=STATUS_TOOL_ERROR,
            detail=f"Antares scanner failed to initialize: {exc}",
        )

    findings: list[SecurityFinding] = []
    dropped = 0
    notes: list[str] = []
    deadline = time.monotonic() + timeout
    for relpath in files:
        if time.monotonic() >= deadline:
            return StageOutcome(
                stage=STAGE_LOCALIZE,
                status=STATUS_TIMEOUT,
                findings=findings,
                dropped_below_floor=dropped,
                detail=f"localization exceeded the per-stage timeout at {relpath}",
            )
        abspath = os.path.join(workdir, relpath)
        if not os.path.isfile(abspath):
            continue
        try:
            if os.path.getsize(abspath) > _MAX_STAGE2_FILE_BYTES:
                notes.append(f"skipped oversized file {relpath}")
                continue
            antares_findings = scanner.scan_file(abspath)
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the stage
            logger.debug("Antares scan failed for %s: %s", relpath, exc)
            continue
        for af in antares_findings:
            confidence = float(getattr(af, "confidence", 0.0))
            if confidence < min_confidence:
                dropped += 1
                continue
            file_field = str(getattr(af, "file", "") or relpath)
            findings.append(
                SecurityFinding(
                    stage=STAGE_LOCALIZE,
                    rule_id=str(getattr(af, "cve_id", "CVE-UNSPECIFIED")),
                    cwe="CWE-UNSPECIFIED",  # Antares localizes; it does not classify
                    file=file_field,
                    line=int(getattr(af, "line", 0)),
                    confidence=confidence,
                    description=str(getattr(af, "description", "")),
                )
            )
    if getattr(scanner, "used_heuristic", False):
        notes.append(
            "Antares ran the keyword fallback (no ML stack installed) — "
            "heuristic findings carry confidence 0.0 and cannot pass the floor"
        )
    detail = "; ".join(notes)
    return StageOutcome(
        stage=STAGE_LOCALIZE,
        status=STATUS_OK,
        findings=findings,
        dropped_below_floor=dropped,
        detail=detail,
    )


# ── Stage 3: judge (full mode only) ──────────────────────────────────────────

_JUDGE_SYSTEM_PROMPT = (
    "You are a security review judge. You receive JSON hypotheses from "
    "deterministic rule scanners and a localization model. For each hypothesis "
    "decide whether it is a REAL vulnerability in the cited code. Reply with "
    "ONLY a JSON array; one object per hypothesis: "
    '{"rule_id": str, "confirmed": bool, "citation": "<relative/path>:<line>", '
    '"note": str}. Every confirmed finding MUST cite the exact file:line as '
    '"relative/path:line". If you cannot cite a file:line, set confirmed to '
    "false — an unciteable hypothesis is unconfirmed, never a pass."
)


def _render_judge_prompt(hypotheses: list[dict[str, Any]]) -> str:
    import textwrap

    body = json.dumps(hypotheses, indent=2)
    return (
        textwrap.dedent(
            """
        Hypotheses from stage 1 (rules) and stage 2 (localization), already
        filtered to the configured confidence floor:

        """
        ).strip()
        + f"\n{body}\n"
    )


def _parse_judge_response(content: str) -> list[dict[str, Any]]:
    """Extract the judge's JSON verdict array; tolerate prose around it."""
    decoder = json.JSONDecoder()
    payload: Any = None
    for match in re.finditer(r"[\[{]", content or ""):
        try:
            payload, _ = decoder.raw_decode(content[match.start() :])
            break
        except json.JSONDecodeError:
            continue
    if isinstance(payload, dict):
        for key in ("verdicts", "findings", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        logger.warning("security review judge returned no parseable JSON verdicts")
        return []
    return [item for item in payload if isinstance(item, dict)]


def run_judge_stage(
    hypotheses: list[dict[str, Any]],
    judge_fn: JudgeFn | None,
    timeout: float,
) -> StageOutcome:
    """Stage 3: the judge sees ONLY the surviving hypotheses (AC4).

    ``judge_fn is None`` means no judge is available — the caller applies
    ``missing_tool_policy``. A confirmed verdict MUST cite ``file:line``
    (``_CITATION_RE``); a hypothesis the judge cannot cite becomes an
    ``unconfirmed`` entry and never a confirmed finding, never a pass.
    """
    if judge_fn is None:
        return StageOutcome(
            stage=STAGE_JUDGE,
            status=STATUS_MISSING_TOOL,
            escalations=0,
            detail=(
                f"{len(hypotheses)} hypothesis/hypotheses identified but no judge "
                "is available (no LLM credential configured)"
            ),
        )
    outcome: list[dict[str, Any]] | None
    try:
        outcome = _run_judge_callable(hypotheses, judge_fn, timeout)
    except _JudgeError as exc:
        return StageOutcome(
            stage=STAGE_JUDGE,
            status=STATUS_TOOL_ERROR,
            escalations=len(hypotheses),
            detail=f"judge callable raised: {exc}",
        )
    if outcome is None:
        return StageOutcome(
            stage=STAGE_JUDGE,
            status=STATUS_TIMEOUT,
            escalations=len(hypotheses),
            detail="judge exceeded the per-stage timeout",
        )

    findings: list[SecurityFinding] = []
    unconfirmed: list[str] = []
    by_rule_id = {str(h.get("rule_id", "")): h for h in hypotheses}
    for verdict in outcome:
        rule_id = str(verdict.get("rule_id", ""))
        confirmed = verdict.get("confirmed") is True
        citation = str(verdict.get("citation", "")).strip()
        if confirmed and _CITATION_RE.match(citation):
            file_part, _, line_part = citation.rpartition(":")
            hypothesis = by_rule_id.get(rule_id, {})
            findings.append(
                SecurityFinding(
                    stage=STAGE_JUDGE,
                    rule_id=rule_id or "judge",
                    cwe=_cwe_of(str(verdict.get("cwe", "") or hypothesis.get("cwe", ""))),
                    file=file_part,
                    line=int(line_part),
                    confidence=float(hypothesis.get("confidence", 1.0)),
                    description=str(verdict.get("note", "")),
                    confirmed=True,
                )
            )
        else:
            label = "no citation provided" if not citation else f"unciteable citation {citation!r}"
            unconfirmed.append(f"{rule_id or 'unknown'}: {label}")
    return StageOutcome(
        stage=STAGE_JUDGE,
        status=STATUS_OK,
        findings=findings,
        escalations=len(hypotheses),
        unconfirmed=unconfirmed,
    )


def _run_judge_callable(
    hypotheses: list[dict[str, Any]],
    judge_fn: JudgeFn,
    timeout: float,
) -> list[dict[str, Any]] | None:
    """Run *judge_fn* in a daemon thread joined under *timeout*.

    Returns None on timeout. The thread is daemon: an abandoned judge call
    never blocks interpreter exit, and the bounded budget is enforced by the
    join rather than by trusting the callable.
    """
    result: list[list[dict[str, Any]] | None] = [None]
    error: list[Exception | None] = [None]

    def _target() -> None:
        try:
            result[0] = judge_fn(hypotheses)
        except Exception as exc:  # noqa: BLE001 - reported as a tool error
            error[0] = exc

    thread = threading.Thread(target=_target, name="security-review-judge", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if error[0] is not None:
        raise _JudgeError(str(error[0])) from error[0]
    return result[0] if result[0] is not None else []


class _JudgeError(RuntimeError):
    """The judge callable raised — surfaced as a stage tool error."""


# ── Orchestration ────────────────────────────────────────────────────────────


def run_staged_review(
    workdir: str,
    files: list[str],
    config: Any,
    judge_fn: "JudgeFn | None | object" = _UNSET,
    *,
    tool_runner: ToolRunner | None = None,
    scanner_factory: ScannerFactory | None = None,
    build_judge: Callable[[Any], JudgeFn | None] | None = None,
) -> StagedReviewOutcome:
    """Run the staged pipeline over *files* per *config*.

    ``files`` is the change set the caller already collected (the guard's
    staged or working-tree scope) — the pipeline never re-derives a scope, so
    staged-diff scoping is the caller's contract (AC6). ``judge_fn`` left
    unset means "build the default"; passing None explicitly means "no judge
    is available". Missing tools and timeouts degrade per
    ``config.missing_tool_policy``: "fail" fails the pipeline (named), "skip"
    turns the stage into a named skip — never a silent pass (AC7).
    """
    mode = getattr(config, "mode", "off")
    if mode == "off":
        return StagedReviewOutcome(mode="off")
    policy = getattr(config, "missing_tool_policy", "fail")
    timeout = float(getattr(config, "per_stage_timeout_seconds", 60))
    floor = float(getattr(config, "min_confidence", 0.7))

    stages: list[StageOutcome] = []

    rules = run_rules_stage(workdir, files, timeout, tool_runner=tool_runner)
    localize = run_localization_stage(
        workdir, files, floor, timeout, scanner_factory=scanner_factory
    )
    stages.extend(_apply_policy([rules, localize], policy))

    hypotheses = [
        *_hypotheses_of(rules, STAGE_RULES, floor),
        *_hypotheses_of(localize, STAGE_LOCALIZE, floor),
    ]

    escalations = 0
    unconfirmed: list[str] = []
    judge_findings: list[SecurityFinding] = []
    if mode == "full":
        if judge_fn is _UNSET:
            builder = build_judge or build_judge_fn
            resolved_judge = builder(config)
        else:
            resolved_judge = judge_fn  # type: ignore[assignment]
        judge = run_judge_stage(hypotheses, resolved_judge, timeout)
        escalations = judge.escalations
        unconfirmed = judge.unconfirmed
        judge_findings = judge.findings
        stages.extend(_apply_policy([judge], policy))

    findings = [
        *rules.findings,
        *localize.findings,
        *judge_findings,
    ]
    failed = [s for s in stages if s.degraded]
    passed = not failed and not findings
    return StagedReviewOutcome(
        mode=str(mode),
        findings=findings,
        stages=stages,
        escalations=escalations,
        unconfirmed=unconfirmed,
        passed=passed,
    )


def _hypotheses_of(stage: StageOutcome, stage_name: str, floor: float) -> list[dict[str, Any]]:
    """Stage findings at or above the floor, as judge-facing hypothesis dicts."""
    return [
        {
            "rule_id": f.rule_id,
            "cwe": f.cwe,
            "file": f.file,
            "line": f.line,
            "confidence": f.confidence,
            "description": f.description,
            "stage": stage_name,
        }
        for f in stage.findings
        if f.confidence >= floor
    ]


def _apply_policy(stages: list[StageOutcome], policy: str) -> list[StageOutcome]:
    """Apply ``missing_tool_policy`` to degraded stages.

    ``fail``: the stage keeps its degraded status — the pipeline fails, named.
    ``skip``: the stage becomes a named SKIP (TRUST-001: never a silent pass);
    findings collected before the degradation are preserved on the outcome.
    """
    if policy != "skip":
        return stages
    resolved: list[StageOutcome] = []
    for stage in stages:
        if stage.degraded:
            resolved.append(
                StageOutcome(
                    stage=stage.stage,
                    status=STATUS_SKIPPED,
                    findings=stage.findings,
                    dropped_below_floor=stage.dropped_below_floor,
                    escalations=stage.escalations,
                    unconfirmed=stage.unconfirmed,
                    detail=f"skipped by missing_tool_policy=skip: {stage.detail or stage.status}",
                    notes=stage.notes,
                )
            )
        else:
            resolved.append(stage)
    return resolved


# ── Rendering ────────────────────────────────────────────────────────────────


def render_outcome(outcome: StagedReviewOutcome, min_confidence: float) -> str:
    """Human-readable lane output; every finding line carries its stage tag."""
    dropped = outcome.dropped_below_floor()
    head = (
        f"security review ({outcome.mode}): {len(outcome.findings)} finding(s), "
        f"{dropped} dropped below confidence {min_confidence}"
    )
    lines = [head]
    for finding in outcome.findings[:20]:
        lines.append(
            f"  {finding.stage_label()} {finding.rule_id} {finding.cwe} "
            f"{finding.location} conf={finding.confidence:.2f} "
            f"({finding.verdict_label()}) {finding.description}"
        )
    if len(outcome.findings) > 20:
        lines.append(f"  ... and {len(outcome.findings) - 20} more")
    for stage in outcome.stages:
        if stage.status == STATUS_SKIPPED:
            lines.append(f"  skipped stage {stage.stage}: {stage.detail}")
        elif stage.degraded:
            lines.append(f"  failed stage {stage.stage} ({stage.status}): {stage.detail}")
        elif stage.detail:
            lines.append(f"  {stage.stage}: {stage.detail}")
    if outcome.unconfirmed:
        lines.append(f"  unconfirmed (judge could not cite file:line): {len(outcome.unconfirmed)}")
        for entry in outcome.unconfirmed[:10]:
            lines.append(f"    - {entry}")
    return "\n".join(lines)


def outcome_summary(outcome: StagedReviewOutcome) -> dict[str, Any]:
    """Machine-readable summary for Tier1Result.extra."""
    return {
        "mode": outcome.mode,
        "findings": [f.to_dict() for f in outcome.findings],
        "stages": {s.stage: s.status for s in outcome.stages},
        "stage_details": {s.stage: s.detail for s in outcome.stages if s.detail},
        "stage_notes": {s.stage: s.notes for s in outcome.stages if s.notes},
        "escalations": outcome.escalations,
        "unconfirmed": outcome.unconfirmed,
        "dropped_below_floor": outcome.dropped_below_floor(),
        "passed": outcome.passed,
    }
