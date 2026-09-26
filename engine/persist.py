"""
Verdict Persister — Save verdicts to .gitreins/history/ with configurable storage.

Storage modes:
    "git"        — auto-commit to the gitreins history ref (default)
    "filesystem" — write files to .gitreins/history/ only, no git commits

Reads: list_verdicts()/count_verdicts() prefer the local .gitreins/history/
files. When the local dir is missing or holds no entries and storage is
"git", they fall back to the verdicts committed on the history REF
(``HISTORY_REF``; a repo written before DF-GITREINS-POC-52 is read through
its legacy ``refs/heads/gitreins`` branch as well, and a fresh clone — which
fetches neither local ref — through their remote-tracking copies,
DF-GITREINS-POC-68) — so a fresh clone (whose working tree has no
.gitreins/history/, since it is gitignored) can still browse the full
verdict history via `gitreins report`.

Config (.gitreins/config.yaml):
    history:
      enabled: true               # false = no persistence at all
      path: ".gitreins/history"   # relative to repo root, or absolute
      storage: "git"              # "git" or "filesystem"
      max_verdicts: 1000          # auto-prune old verdicts

Usage:
    persister = VerdictPersister(workdir="/path/to/repo")
    commit_hash = persister.persist(task_id, verdict_data)

Every entry point that produces a verdict (CLI sync ``judge``, CLI async
``judge --async``, MCP ``judge.evaluate``, MCP ``task.complete``) persists it
through the shared :func:`persist_evaluation` helper below, so the verdict
record on disk is identical no matter which surface ran the evaluation.
Console printing deliberately does NOT live here — the MCP server's stdout is
its JSON-RPC channel and a stray write corrupts the protocol.

One LIVE record per job: the entry directory is keyed on timestamp + task id,
so a job that is re-dispatched under the SAME id — the resume path re-runs an
orphaned ``running`` job whose owner died after it had already persisted —
would otherwise append a second verdict for one logical run, leaving a consumer
that joins history to the job store unable to tell which record is graded.
:meth:`VerdictPersister.persist` therefore supersedes the previous live entry
for a job id instead of leaving both looking current: the older record gets
``superseded_by`` (the path of the record that replaced it) and
``superseded_at``; the newer one carries ``supersedes`` (the path it replaced,
``null`` for a first attempt). Superseding is a LABEL, never a delete — the
abandoned attempt keeps its own verdict, summary and evidence for the audit
trail. Records without a ``job_id`` (the sync surfaces) have no stable run
identity to key on, so they are never superseded.

Resolution-gate records (DF-GITREINS-POC-36): ``gitreins resolve``,
``gitreins preflight`` and the MCP ``context.resolve`` tool file their verdicts
in this SAME store through :func:`persist_resolution`, so ``gitreins report``
and ``gitreins serve`` show the gate's decisions instead of a hole. A
resolution record is not a task verdict — it carries ``kind: "resolution"``
plus a ``source``, and no ``passed``/criteria — so readers that COUNT judgments
(the pass/fail rollups in :func:`build_report` and ``/api/stats``) key on that
marker and never let a resolution band read as a graded task.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone

from engine import usage
from engine.repo_paths import WorktreeResolutionError, resolve_worktree_identity

logger = logging.getLogger("gitreins.persist")


def _as_utc(when: datetime) -> datetime:
    """A writer stamp as naive UTC — the shape ``evaluated_at`` has always had.

    Readers treat the stamp as UTC (``gitreins/serve.py`` ``_epoch``), so an
    aware datetime from a caller is converted rather than reformatted with an
    offset that would only some readers understand.
    """
    if when.tzinfo is None:
        return when
    return when.astimezone(timezone.utc).replace(tzinfo=None)


def _as_epoch(when: datetime) -> float:
    """Epoch seconds for a stamp, read exactly the way the readers read it."""
    return _as_utc(when).replace(tzinfo=timezone.utc).timestamp()


# ── Persistence config defaults ────────────────────────────────

DEFAULT_HISTORY_CONFIG = {
    "enabled": True,
    "path": ".gitreins/history",
    "storage": "git",
    "max_verdicts": 1000,
}


def load_history_config(workdir: str) -> dict:
    """Load history section from .gitreins/config.yaml, merged with defaults."""
    config = {}
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if os.path.isfile(config_path):
        try:
            import yaml

            with open(config_path, "r") as f:
                raw = yaml.safe_load(f) or {}
            config = raw.get("history", {})
        except Exception:
            pass

    merged = dict(DEFAULT_HISTORY_CONFIG)
    if isinstance(config, dict):
        for key in merged:
            if key in config:
                merged[key] = config[key]
    return merged


# ── Persister ──────────────────────────────────────────────────

# ── Verdict-history refs (DF-GITREINS-POC-52, fresh-clone fallback POC-68) ──
#
# The verdict history is a REF, not a working branch.  It used to live on
# ``refs/heads/gitreins``, whose name is a path-prefix of the fleet's own
# per-task branches ``refs/heads/gitreins/task/<id>`` (BRANCH_PREFIX in
# engine/worktree_manager.py).  Git refuses to create a ref that is a prefix
# of an existing one — "cannot lock ref refs/heads/gitreins:
# refs/heads/gitreins/task/fix-add exists; cannot create refs/heads/gitreins"
# — so in ANY repo that had ever run a fleet lane the verdict-history commit
# silently degraded to "dry-run": verdict.json on disk, nothing in git, no
# audit trail and no cross-clone fallback for report/serve.
#
# The collision class exists only between refs git may create under
# ``refs/heads/``.  The history therefore lives in its own namespace, outside
# the branch namespace: nothing in GitReins (or the fleet) creates a ref under
# ``refs/gitreins/``, and no branch name — shipped today or added later — can
# become a prefix of it or have it as a prefix.  Being outside refs/heads also
# keeps it out of ``git branch`` lists and unreachable by a checkout, which is
# right for a store that is never checked out.
#
# Read it with the full name (``git show refs/gitreins/history:<path>``) or the
# DWIM shorthand ``gitreins/history:<path>``; both resolve the same ref.
HISTORY_REF = "refs/gitreins/history"

# Clones written before the move keep their history on this branch.  Reads
# consult BOTH refs (union, deduped by entry path) so a repo that predates the
# move keeps every verdict it ever filed, and the first write after the upgrade
# SEEDS the new ref from the legacy tip (see _git_commit) so the old history
# rides along instead of being stranded.  Nothing here deletes the legacy
# branch; a repo that wants a single ref can migrate with:
#     git update-ref refs/gitreins/history refs/heads/gitreins
#     git branch -D gitreins          # only once a copy exists elsewhere
LEGACY_HISTORY_REF = "refs/heads/gitreins"

# Fresh-clone fallbacks (DF-GITREINS-POC-68).  ``git clone`` maps only
# ``refs/heads/*`` to ``refs/remotes/origin/*``, so a fresh clone has NO local
# verdict-history ref: the canonical ref is not fetched by default and the
# legacy branch appears only as its remote-tracking copy — the reader used to
# print "No verdict history found." in exactly that shape.  The remote-tracking
# refs are the LAST resorts (after both local refs): a clone that later gains
# local refs must prefer them, and among the fallbacks the canonical copy
# outranks the legacy one.  See README "Verdict History" for the fetch config
# that makes the canonical ref visible to a clone.
REMOTE_HISTORY_REF = "refs/remotes/origin/gitreins/history"
REMOTE_LEGACY_HISTORY_REF = "refs/remotes/origin/gitreins"


class VerdictPersister:
    """Persist verdict results and provide history lookup for reports."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self.config = load_history_config(self.workdir)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def history_dir(self) -> str:
        path = self.config.get("path", ".gitreins/history")
        if os.path.isabs(path):
            return path
        return os.path.join(self.workdir, path)

    @property
    def storage_mode(self) -> str:
        return self.config.get("storage", "git")

    # ── Save ────────────────────────────────────────────────

    def persist(
        self,
        task_id: str,
        verdict_data: dict,
        collect_evidence: Callable[[str], dict] | None = None,
        *,
        evaluated_at: datetime | None = None,
    ) -> str:
        """Save verdict to history. Returns commit hash or "dry-run" or "disabled".

        ``collect_evidence`` is an optional ``f(entry_dir) -> manifest`` hook run
        after the verdict directory exists and before it is committed, so the
        worker-brief/driver-log/patch artifacts land in the SAME history commit
        as the verdict they belong to. A hook that raises is ignored: evidence
        is never allowed to fail a verdict.

        ``evaluated_at`` is the stamp the record carries (naive UTC, defaulting
        to now). It is keyword-only and optional — a caller that has already
        timestamped something belonging to this record (the resolution gate
        stamps its usage row with it, DF-GITREINS-POC-36) hands the SAME instant
        in, so the two artifacts agree to the microsecond instead of depending on
        which of two clock reads happened first.

        A record carrying a ``job_id`` supersedes the previous LIVE entry for
        that same job id (DF-GITREINS-POC-26): the predecessor is located before
        the new entry is written and marked once it has landed, so after a
        resumed job exactly ONE record per job id is live and the abandoned
        attempt names the record that replaced it.
        """
        if not self.enabled:
            return "disabled"

        verdict_data["task_id"] = task_id
        verdict_data["evaluated_at"] = _as_utc(evaluated_at or datetime.utcnow()).isoformat()

        # Generate deterministic short hash
        hash_input = f"{task_id}:{verdict_data['evaluated_at']}"
        short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]

        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        entry_dir = os.path.join(self.history_dir, date_str, short_hash)
        os.makedirs(entry_dir, exist_ok=True)

        # Supersede contract: locate the record this one replaces BEFORE writing,
        # then label it AFTER the new record is on disk. Write-first ordering is
        # deliberate — a crash in between leaves the previous attempt live rather
        # than leaving a job id with no live record at all.
        job_id = verdict_data.get("job_id")
        prior_entry_rel = self._find_live_entry_for_job(job_id) if job_id else None
        new_entry_path = self._entry_record_path(f"{date_str}/{short_hash}")
        verdict_data["supersedes"] = (
            self._entry_record_path(prior_entry_rel) if prior_entry_rel else None
        )
        verdict_data["superseded_by"] = None

        # Worker execution evidence (JVIEW-005): the brief, the driver-log tail
        # and the graded patch, written next to verdict.json so the verdict
        # directory is a self-contained audit unit.
        if collect_evidence is not None:
            try:
                manifest = collect_evidence(entry_dir)
            except Exception:
                manifest = None
            if manifest:
                verdict_data["evidence"] = manifest

        # Write verdict.json
        verdict_path = os.path.join(entry_dir, "verdict.json")
        with open(verdict_path, "w") as f:
            json.dump(verdict_data, f, indent=2, default=str)

        # Label the attempt this one replaced (never delete it — the audit trail
        # of the interrupted run survives).
        if prior_entry_rel is not None:
            self._mark_superseded(prior_entry_rel, new_entry_path, verdict_data["evaluated_at"])

        # Write summary.md
        summary_path = os.path.join(entry_dir, "summary.md")
        summary = self._build_summary(task_id, verdict_data)
        with open(summary_path, "w") as f:
            f.write(summary)

        # Git commit if configured
        commit_hash = "dry-run"
        if self.storage_mode == "git":
            commit_hash = self._git_commit(
                entry_dir,
                task_id,
                verdict_data.get("passed", False),
                subject=self._history_subject(task_id, verdict_data),
            )

        # Prune old verdicts if over max
        self._prune_old()

        return commit_hash

    # ── List / Report ────────────────────────────────────────

    def list_verdicts(self, n: int = 20, task_id: str | None = None) -> list[dict]:
        """Return recent verdicts as a list of dicts, newest first.

        Local filesystem entries take precedence. When the local history
        dir is missing or holds no verdict.json files at all and storage
        mode is "git", falls back to the verdict refs — canonical, then
        legacy, then their remote-tracking copies — so fresh clones (no
        local .gitreins/history/, it is gitignored; no local history ref,
        ``git clone`` fetches neither by default) can still browse history
        via `gitreins report`.
        """
        entries = self._list_local_verdicts(n=n, task_id=task_id)
        if entries:
            return entries
        # Local pass yielded nothing: fall back to the verdict refs only
        # when git storage is active AND the local dir holds no
        # verdict.json files at all (a task_id filter that matches nothing
        # locally must not pull ref entries in — sources are never merged).
        if self.storage_mode == "git" and not self._local_history_has_entries():
            return self._list_branch_verdicts(n=n, task_id=task_id)
        return []

    def _list_local_verdicts(self, n: int = 20, task_id: str | None = None) -> list[dict]:
        """Read verdict entries from the local filesystem (original behavior)."""
        if not os.path.isdir(self.history_dir):
            return []

        entries = []
        for date_dir in sorted(os.listdir(self.history_dir), reverse=True):
            date_path = os.path.join(self.history_dir, date_dir)
            if not os.path.isdir(date_path):
                continue
            for hash_dir in sorted(os.listdir(date_path), reverse=True):
                entry_dir = os.path.join(date_path, hash_dir)
                verdict_path = os.path.join(entry_dir, "verdict.json")
                if not os.path.isfile(verdict_path):
                    continue
                try:
                    with open(verdict_path) as f:
                        data = json.load(f)
                    if task_id and data.get("task_id") != task_id:
                        continue
                    data["_date"] = date_dir
                    data["_hash"] = hash_dir
                    entries.append(data)
                    if len(entries) >= n:
                        return entries
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    # QA-GITREINS-POC-6: an undecodable verdict.json is skipped
                    # exactly like a malformed one — the sibling branch reader
                    # (``_list_branch_verdicts``) already tolerated this; the
                    # local reader used to abort the whole listing on it.
                    continue

        return entries

    def _local_history_has_entries(self) -> bool:
        """True when the local history dir holds at least one verdict.json."""
        if not os.path.isdir(self.history_dir):
            return False
        for date_dir in os.listdir(self.history_dir):
            date_path = os.path.join(self.history_dir, date_dir)
            if not os.path.isdir(date_path):
                continue
            for hash_dir in os.listdir(date_path):
                if os.path.isfile(os.path.join(date_path, hash_dir, "verdict.json")):
                    return True
        return False

    def _branch_history_prefix(self) -> str:
        """History path relative to workdir, git-style (forward slashes).

        Verdicts are committed to the history ref (HISTORY_REF) at the path
        relative to the repo root (persist() commits os.path.relpath of the
        entry dir), so this is the prefix to enumerate on that ref.
        """
        return os.path.relpath(self.history_dir, self.workdir).replace(os.sep, "/")

    @staticmethod
    def _history_refs() -> list[str]:
        """Refs that hold verdict history, most authoritative first.

        Four refs can legitimately hold history at once: the current scheme's
        canonical ref, the legacy branch in a repo written before
        DF-GITREINS-POC-52, and — in a fresh clone, where ``git clone`` maps
        only ``refs/heads/*`` to ``refs/remotes/origin/*`` and so fetches
        NEITHER local history ref — their remote-tracking copies.  Readers
        union them (deduped by entry path, earlier refs winning) so nothing a
        repo already filed disappears from report/serve after the move, and a
        fresh clone serves its origin's history instead of degrading to
        "No verdict history found."
        """
        return [
            HISTORY_REF,
            LEGACY_HISTORY_REF,
            REMOTE_HISTORY_REF,
            REMOTE_LEGACY_HISTORY_REF,
        ]

    def _ls_tree_verdict_paths(self, ref: str, prefix: str) -> list[str]:
        """Verdict.json paths on *ref* under *prefix*; [] when the ref is absent.

        Never raises: a missing ref, a non-repo workdir and a git timeout all
        read as "no history here", so callers degrade to "No verdict history
        found." exactly as before.
        """
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", ref, "--", prefix],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("git ls-tree failed on %s (non-fatal): %s", ref, e)
            return []
        if result.returncode != 0:
            return []  # ref missing or not a git repo
        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().endswith("/verdict.json")
        ]

    def _branch_entry_paths(self) -> list[tuple[str, str]]:
        """``[(ref, path)]`` for every verdict.json on the history refs, newest first.

        The union of all history refs (canonical, legacy, remote-tracking —
        see ``_history_refs``), deduped by entry path — a more authoritative
        ref wins when several carry the same path.
        """
        prefix = self._branch_history_prefix()
        holder: dict[str, str] = {}
        for ref in self._history_refs():
            for path in self._ls_tree_verdict_paths(ref, prefix):
                holder.setdefault(path, ref)
        # Paths are <prefix>/<date>/<hash>/verdict.json — reverse lexical order
        # matches the local reader's newest-first (date desc, hash desc).
        return [(path, holder[path]) for path in sorted(holder, reverse=True)]

    def _read_branch_verdict(self, ref: str, path: str) -> dict | None:
        """Parse one verdict.json off *ref*; None when it cannot be read."""
        try:
            show = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("Failed to read verdict %s from %s (non-fatal): %s", path, ref, e)
            return None
        if show.returncode != 0:
            return None
        try:
            return json.loads(show.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Failed to parse verdict %s from %s (non-fatal): %s", path, ref, e)
            return None

    def _list_branch_verdicts(self, n: int = 20, task_id: str | None = None) -> list[dict]:
        """Read verdict entries committed to the history ref(s).

        Enumerates verdict.json files under the history path on each history
        ref (canonical, then the legacy branch, then the remote-tracking
        fallbacks a fresh clone has) and reads each with ``git show
        <ref>:<path>``. Returns [] on any git failure (refs absent, not a git
        repo, timeout) — callers degrade to "No verdict history found."
        exactly as before.
        """
        prefix = self._branch_history_prefix()
        rel_prefix = prefix.rstrip("/") + "/"

        entries = []
        for path, ref in self._branch_entry_paths():
            rel = path[len(rel_prefix) :] if path.startswith(rel_prefix) else path
            parts = rel.split("/")
            if len(parts) != 3 or parts[2] != "verdict.json":
                continue
            date_dir, hash_dir = parts[0], parts[1]
            data = self._read_branch_verdict(ref, path)
            if not isinstance(data, dict):
                continue
            if task_id and data.get("task_id") != task_id:
                continue
            data["_date"] = date_dir
            data["_hash"] = hash_dir
            # Which ref holds this entry: a consumer that has to NAME it for a
            # human (worktree_manager's verdict reference) must name the ref it
            # is actually readable from, not assume the current one.
            data["_ref"] = ref
            entries.append(data)
            if len(entries) >= n:
                break

        return entries

    def count_verdicts(self) -> int:
        """Return total number of stored verdict entries.

        Local filesystem entries take precedence; when the local history
        dir is missing or empty and storage mode is "git", counts the
        verdict.json files on the history ref(s) instead.
        """
        count = 0
        if os.path.isdir(self.history_dir):
            for date_dir in os.listdir(self.history_dir):
                date_path = os.path.join(self.history_dir, date_dir)
                if os.path.isdir(date_path):
                    count += len(
                        [
                            d
                            for d in os.listdir(date_path)
                            if os.path.isdir(os.path.join(date_path, d))
                        ]
                    )
        if count == 0 and self.storage_mode == "git":
            return self._count_branch_verdicts()
        return count

    def _count_branch_verdicts(self) -> int:
        """Count verdict.json files on the history refs (no content reads)."""
        return len(self._branch_entry_paths())

    # ── Internal ─────────────────────────────────────────────

    def _build_summary(self, task_id: str, verdict_data: dict) -> str:
        # Resolution records are not graded tasks: they have no pass/fail, no
        # criteria and no pipeline stages, so the judge template would print a
        # fabricated "✗ FAIL" for a band that simply says UNRESOLVED.
        if verdict_data.get("kind") == KIND_RESOLUTION:
            return self._build_resolution_summary(task_id, verdict_data)
        passed = verdict_data.get("passed", False)
        verdict = verdict_data.get("verdict", None)
        task_title = verdict_data.get("task_title", task_id)
        items = verdict_data.get("items", [])
        stages = verdict_data.get("stages", {})
        summary_text = verdict_data.get("summary", "")
        evaluated_at = verdict_data.get("evaluated_at", "")

        lines = []
        lines.append(f"# Verdict: {task_id}")
        lines.append("")
        lines.append(f"**Task:** {task_title}")
        lines.append(f"**Evaluated:** {evaluated_at}")
        lines.append(f"**Result:** {'✓ PASS' if passed else '✗ FAIL'}")
        if verdict:
            verdict_str = verdict.verdict if hasattr(verdict, "verdict") else str(verdict)
            lines.append(f"**Verdict:** {verdict_str}")
        lines.append("")

        # Items from evaluator
        if items:
            lines.append("## Criteria")
            lines.append("")
            for item in items:
                if isinstance(item, dict):
                    status = "✓" if item.get("status") == "PASS" else "✗"
                    lines.append(f"- {status} **{item.get('criterion', '?')}**")
                    detail = item.get("detail", "")
                    if detail:
                        lines.append(f"  - {detail}")
                else:
                    status = "✓" if getattr(item, "status", None) == "PASS" else "✗"
                    criterion = getattr(item, "criterion", str(item))
                    detail = getattr(item, "detail", "")
                    lines.append(f"- {status} **{criterion}**")
                    if detail:
                        lines.append(f"  - {detail}")
            lines.append("")

        # Pipeline stages
        if stages:
            lines.append("## Pipeline Stages")
            lines.append("")
            for stage_id, stage in stages.items():
                stage_passed = stage.get("passed", False)
                icon = "✓" if stage_passed else "✗"
                lines.append(f"- {icon} **{stage_id}**")
                stage_summary = stage.get("summary", "")
                if stage_summary:
                    lines.append(f"  - {stage_summary}")
            lines.append("")

        # Free-form summary
        if summary_text:
            lines.append("## Summary")
            lines.append("")
            lines.append(summary_text)
            lines.append("")

        return "\n".join(lines)

    def _build_resolution_summary(self, task_id: str, verdict_data: dict) -> str:
        """Summary for a resolution record — band, question, bundle, tokens.

        Deliberately NOT the judge template: a resolution record has no
        criteria, no stages and no pass/fail, so it reports the band, the
        question the gate was asked, where the verdict came from, the bundle
        that grounded it and the tokens the call actually spent.
        """
        payload = verdict_data.get("verdict")
        if not isinstance(payload, dict):
            payload = {}
        band = verdict_data.get("band") or payload.get("verdict") or "RESOLUTION"
        question = (
            verdict_data.get("question")
            or payload.get("question")
            or verdict_data.get("task_title")
            or task_id
        )
        lines = [
            f"# Resolution gate: {band}",
            "",
            f"**Question:** {question}",
            f"**Surface:** {verdict_data.get('source') or '?'}",
            f"**Evaluated:** {verdict_data.get('evaluated_at', '')}",
        ]
        probability = verdict_data.get("probability")
        if isinstance(probability, (int, float)):
            lines.append(f"**Probability:** {probability:.3f}")
        if verdict_data.get("missing_kind"):
            lines.append(f"**Missing:** {verdict_data['missing_kind']}")

        manifest = payload.get("manifest") or []
        if manifest:
            lines.append("")
            lines.append("## Bundle")
            lines.append("")
            for entry in manifest:
                if isinstance(entry, dict):
                    lines.append(
                        f"- `{entry.get('file', '?')}` "
                        f"({entry.get('provenance', '?')}, {entry.get('bytes', 0)}B)"
                    )

        tokens_in = payload.get("input_tokens")
        tokens_out = payload.get("output_tokens")
        if tokens_in is not None or tokens_out is not None:
            lines.append("")
            lines.append(f"**Tokens:** input={tokens_in} output={tokens_out}")

        notes = payload.get("notes") or []
        if notes:
            lines.append("")
            lines.append("## Notes")
            lines.append("")
            lines.extend(f"- {note}" for note in notes)

        return "\n".join(lines) + "\n"

    # ── Supersede bookkeeping (DF-GITREINS-POC-26) ───────────

    def _entry_record_path(self, entry_rel: str) -> str:
        """Workdir-relative, "/"-joined path for a history-relative entry path.

        This is the shape stored in ``supersedes``/``superseded_by``: the path a
        verdict entry has on the ``gitreins`` branch, which is also how a record
        is named on disk. An entry that cannot be expressed relative to the
        workdir (history configured outside it) is recorded by absolute path
        instead — still an unambiguous pointer for a consumer.
        """
        abs_entry = os.path.join(self.history_dir, *entry_rel.split("/"))
        rel_to_wd = os.path.relpath(abs_entry, self.workdir)
        if rel_to_wd.startswith(".."):
            return abs_entry.replace(os.sep, "/")
        return rel_to_wd.replace(os.sep, "/")

    def _find_live_entry_for_job(self, job_id: str) -> str | None:
        """History-relative path of the newest LIVE entry carrying *job_id*.

        "Live" means the record is not marked ``superseded_by``. The scan runs
        newest first (date desc, hash desc — the reader's ordering), so a chain
        of resumes always supersedes the record that is currently live. Records
        that are malformed or undecodable are skipped exactly as the history
        reader skips them, and a record written by another surface for the same
        job id is found the same way (the store is shared).
        """
        if not os.path.isdir(self.history_dir):
            return None
        for date_dir in sorted(os.listdir(self.history_dir), reverse=True):
            date_path = os.path.join(self.history_dir, date_dir)
            if not os.path.isdir(date_path):
                continue
            for hash_dir in sorted(os.listdir(date_path), reverse=True):
                verdict_path = os.path.join(date_path, hash_dir, "verdict.json")
                if not os.path.isfile(verdict_path):
                    continue
                try:
                    with open(verdict_path) as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    continue
                if data.get("job_id") == job_id and not data.get("superseded_by"):
                    return f"{date_dir}/{hash_dir}"
        return None

    def _mark_superseded(self, entry_rel: str, superseded_by: str, when: str) -> None:
        """Label the entry at *entry_rel* as superseded by *superseded_by*.

        Written atomically (tmp file + ``os.replace``) so a concurrent reader
        never sees a half-rewritten verdict. Never raises: this is bookkeeping
        on top of a verdict that is already written, so a failure must not turn
        a successful persist into an error.
        """
        try:
            verdict_path = os.path.join(self.history_dir, *entry_rel.split("/"), "verdict.json")
            with open(verdict_path) as f:
                data = json.load(f)
            data["superseded_by"] = superseded_by
            data["superseded_at"] = when
            tmp = f"{verdict_path}.tmp-{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, verdict_path)
        except Exception as exc:
            logger.warning("Failed to mark %s superseded (non-fatal): %s", entry_rel, exc)

    def _git_commit(
        self,
        entry_dir: str,
        task_id: str,
        passed: bool,
        subject: str | None = None,
    ) -> str:
        """Commit verdict entry to the history ref. Returns short hash or 'dry-run'.

        Both the create and the append case are pure plumbing (hash-object /
        mktree, or read-tree in a throwaway index / write-tree, then
        commit-tree / update-ref), so the caller's HEAD, index and working tree
        are never touched — the property DF-GITREINS-POC-1 established. The ref
        lives outside ``refs/heads`` (HISTORY_REF), so a repo whose branches
        include the fleet's own ``gitreins/task/<id>`` can still commit its
        verdict history.
        """
        git_dir = os.path.join(self.workdir, ".git")
        if not os.path.exists(git_dir):
            logger.warning("No .git directory — verdict files written but not committed")
            return "dry-run"

        try:
            rel_path = os.path.relpath(entry_dir, self.workdir)
            message = subject or f"verdict: {task_id} — {'PASS' if passed else 'FAIL'}"

            current = self._resolve_ref(HISTORY_REF)
            if current is not None:
                parent = current
            else:
                # First entry under the new ref. A repo written before
                # DF-GITREINS-POC-52 still carries its history on the legacy
                # branch: chain onto that tip so the entries already filed stay
                # in the log instead of starting a second, stranded root (the
                # same thing the documented one-liner does —
                # ``git update-ref refs/gitreins/history refs/heads/gitreins``).
                parent = self._resolve_ref(LEGACY_HISTORY_REF)

            commit = self._commit_entry(rel_path, message, parent)

            # All-zeros <old-oid> makes update-ref refuse if the ref sprang into
            # existence concurrently (first verdict must be the sole creator);
            # otherwise the <old-oid> we read is what guards a racing writer
            # from dropping a verdict chain.
            self._git(["update-ref", HISTORY_REF, commit, current or "0" * 40])

            return commit[:8]

        except subprocess.TimeoutExpired:
            logger.warning("Git command timed out — files written but not committed")
            return "dry-run"
        except Exception as e:
            logger.warning("Git operation failed (non-fatal): %s", e)
            return "dry-run"

    def _history_subject(self, task_id: str, verdict_data: dict) -> str:
        """Commit subject for one history entry, judge or resolution record.

        The ``gitreins`` branch's log is part of the audit trail, so a
        resolution record names its band instead of borrowing the judge's
        PASS/FAIL — a record with no pass/fail must not claim one in git log.
        """
        if verdict_data.get("kind") == KIND_RESOLUTION:
            return f"resolution: {task_id} — {verdict_data.get('band') or 'RESOLUTION'}"
        passed = verdict_data.get("passed", False)
        return f"verdict: {task_id} — {'PASS' if passed else 'FAIL'}"

    def _commit_entry(self, rel_path: str, message: str, parent: str | None) -> str:
        """Commit object holding the parent history tree plus the new entry.

        The entry is the ONLY thing added to the tree, so a history commit can
        never carry the repo's own files: the first verdict (no parent) roots a
        tree holding just its own entry, and every later one extends the
        previous tree.
        """
        tree = self._tree_with_entry(parent, rel_path)
        if parent is None:
            return self._git(["commit-tree", tree, "-m", message])
        return self._git(["commit-tree", tree, "-p", parent, "-m", message])

    def _tree_with_entry(self, parent: str | None, rel_path: str) -> str:
        """Tree of *parent* (empty when None) plus the entry at *rel_path*.

        One mechanism for both the first verdict and every later one: a
        throwaway index (GIT_INDEX_FILE, so the caller's index is untouched)
        loaded from the parent's tree — every earlier entry survives — with the
        entry dir added from disk at its real path.

        This replaced two writers that each had a hole (DF-GITREINS-POC-52): the
        branch-checkout worktree could not update a ref living outside
        ``refs/heads/`` at all, and its plain ``git add`` refused the entry
        whenever the repo ships ``.gitignore``'s ``.gitreins/history/`` rule
        (every GitReins repo does); the mktree root-commit builder dropped the
        path prefix, so the first verdict landed at the tree ROOT and no
        branch-backed reader (which filters on the history path) could ever see
        it.
        """
        index_dir = tempfile.mkdtemp(prefix="gitreins-idx-")
        env = self._git_env()
        # Redirecting GIT_INDEX_FILE keeps every index write (read-tree, add,
        # write-tree) inside the throwaway file: the caller's index stays
        # byte-identical, which is what DF-GITREINS-POC-1 was about.
        env["GIT_INDEX_FILE"] = os.path.join(index_dir, "index")
        try:
            self._git(
                ["read-tree", "--empty"] if parent is None else ["read-tree", parent], env=env
            )
            # -f because .gitreins/history/ is gitignored: the ref IS the
            # versioned copy, so the working-tree store has to be forced in.
            self._git(["add", "-f", "--", rel_path], env=env)
            return self._git(["write-tree"], env=env)
        finally:
            shutil.rmtree(index_dir, ignore_errors=True)

    def _resolve_ref(self, ref: str) -> str | None:
        """Commit oid *ref* points at, or None when the ref does not exist.

        ``--verify --quiet`` keeps an absent ref from printing to stderr; a
        target that is not a commit reads as absent too, since only a commit can
        parent a history entry.
        """
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.workdir,
            env=self._git_env(),
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _git(self, args: list[str], env: dict | None = None) -> str:
        """Run a git plumbing command in the repo and return stripped stdout.

        *env* replaces the plumbing environment wholesale — the tree-building
        path hands in an environment whose GIT_INDEX_FILE points at a throwaway
        index, so its index writes never reach the caller's index. It defaults
        to :meth:`_git_env`.

        Non-zero exits raise RuntimeError with git's stderr — verdict
        persistence must fail loudly rather than report success while the
        history was not written.
        """
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.workdir,
            env=self._git_env() if env is None else env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout.strip()

    @staticmethod
    def _git_env() -> dict:
        """Env for plumbing git calls; identity only when the user has none.

        setdefault on a private copy — an existing user/global identity is
        never overridden, a bare container/test environment still commits.
        """
        env = dict(os.environ)
        env.setdefault("GIT_AUTHOR_NAME", "GitReins")
        env.setdefault("GIT_AUTHOR_EMAIL", "gitreins@localhost")
        env.setdefault("GIT_COMMITTER_NAME", "GitReins")
        env.setdefault("GIT_COMMITTER_EMAIL", "gitreins@localhost")
        return env

    def _prune_old(self) -> None:
        """Remove oldest verdict entries if over max_verdicts."""
        max_v = self.config.get("max_verdicts", 1000)
        if max_v <= 0:
            return  # no pruning

        current = self.count_verdicts()
        if current <= max_v:
            return

        # Collect all entries sorted oldest-first
        all_entries = self.list_verdicts(n=current + 1000)
        all_entries.reverse()  # oldest first

        to_remove = current - max_v
        removed = 0
        for entry in all_entries:
            if removed >= to_remove:
                break
            entry_dir = os.path.join(
                self.history_dir,
                entry.get("_date", ""),
                entry.get("_hash", ""),
            )
            if os.path.isdir(entry_dir):
                try:
                    shutil.rmtree(entry_dir)
                    removed += 1
                except OSError:
                    pass


# ── Shared verdict persistence (CLI + MCP) ─────────────────────


def _verdict_item_dict(item) -> dict:
    """One verdict item as persisted — attribution keys only when present.

    The three-key shape is today's contract; ``resolution_probability`` and
    ``cited_path`` (JEVRES-004) appear only when the item actually carries
    them, so a verdict from a degraded run serializes byte-identically to the
    pre-JEVRES-004 shape.
    """
    d = {"criterion": item.criterion, "status": item.status, "detail": item.detail}
    probability = getattr(item, "resolution_probability", None)
    cited = getattr(item, "cited_path", None)
    if probability is not None:
        d["resolution_probability"] = probability
    if cited:
        d["cited_path"] = cited
    return d


def build_verdict_data(workdir: str, task, result) -> dict:
    """Build the verdict payload persisted for an evaluation.

    Behaviour moved verbatim from the CLI's historical ``_persist_result``:
    same keys, same values, and the same explicit empty-branch metadata for
    detached / non-Git-compatible invocations so the persisted schema stays
    stable while old verdicts stay readable.
    """
    # Stamp the checkout that produced the verdict.  Keep explicit empty
    # branch metadata for detached/non-Git-compatible invocations so the
    # persisted schema remains stable while old verdicts stay readable.
    try:
        identity = resolve_worktree_identity(workdir)
        producing_worktree = str(identity.worktree_root)
        producing_branch = identity.branch or ""
    except WorktreeResolutionError:
        producing_worktree = os.path.abspath(workdir)
        producing_branch = ""

    # The commit stamp is mandatory for merge-back to distinguish a verdict
    # for an older branch tip.
    source_commit = ""
    try:
        commit_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=5,
            check=False,
        )
        if commit_result.returncode == 0:
            source_commit = commit_result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    verdict_data = {
        "task_id": task.id,
        "task_title": task.title,
        "task_criteria": task.criteria,
        "passed": result.passed,
        "worktree": producing_worktree,
        "branch": producing_branch,
        "commit": source_commit,
    }

    # Extract items from verdict or pipeline result. JEVRES-004: items carry
    # the per-criterion resolution attribution when a pre-screen ran; the
    # fields are omitted entirely when absent so verdicts from degraded
    # (ABSTAIN) or pre-screen-era runs keep today's exact shape.
    if result.verdict and hasattr(result.verdict, "items"):
        verdict_data["items"] = [_verdict_item_dict(item) for item in result.verdict.items]
    else:
        verdict_data["items"] = []

    # JEVRES-004 tier 1.5: the full pre-screen (per-criterion probabilities,
    # missing kinds, evidence quality, and the engine's own verdict with its
    # bundle manifest) persists INSIDE the existing verdict record, so
    # `gitreins serve` shows it with the same entry it already lists — no new
    # persistence surface to drift (DF-GITREINS-POC-23).
    prescreen = getattr(result.verdict, "prescreen", None)
    if prescreen:
        verdict_data["prescreen"] = prescreen

    # Pipeline stages
    if result.pipeline_result:
        verdict_data["stages"] = result.pipeline_result.get("stages", {})

    # Summary text
    verdict_data["summary"] = result.summary

    return verdict_data


def persist_evaluation(
    workdir: str,
    task,
    result,
    *,
    extra: dict | None = None,
    collect_evidence: Callable[[str], dict] | None = None,
) -> str:
    """Persist one evaluation verdict through the shared persistence path.

    Returns the verdict commit hash, ``"dry-run"`` (files written, git
    unavailable) or ``"disabled"`` (history switched off). ``extra`` is a
    plain dict stamped into the verdict record — the MCP callers use it to
    record the job id and the surface that produced the verdict.

    Non-fatal by contract: a persistence failure is logged and reported as
    ``"error"``, never raised into the run that produced the verdict. This
    writer never prints: the MCP server's stdout carries JSON-RPC, so console
    output belongs to the CLI wrapper only.
    """
    try:
        persister = VerdictPersister(workdir)
        if not persister.enabled:
            return "disabled"

        verdict_data = build_verdict_data(workdir, task, result)
        if extra:
            verdict_data.update(extra)
        return persister.persist(task.id, verdict_data, collect_evidence=collect_evidence)
    except Exception as exc:  # persistence must never fail the verdict
        logger.warning("Failed to persist verdict for %s (non-fatal): %s", workdir, exc)
        return "error"


# ── Shared resolution-gate persistence (DF-GITREINS-POC-36) ────

#: ``kind`` marker on every resolution-gate record. A judge verdict carries no
#: ``kind`` key at all, so a reader can separate the two instead of inferring
#: the difference from which fields happen to be missing.
KIND_RESOLUTION = "resolution"

#: The id resolution records are filed under — a NAMESPACE, not a task. No task
#: with this id exists and ``gitreins task`` never creates one, so a record
#: never claims a task id it did not evaluate.
RESOLUTION_ENTRY_ID = "resolution"

#: ``step`` value of the usage row a resolution run appends. The judge's step is
#: its pipeline step id (``tier2``); naming this one lets a cost reader tell the
#: gate's calls from the judge's inside the one telemetry file.
RESOLUTION_USAGE_STEP = "resolution"


def _is_decision(verdict) -> bool:
    """True only for a real band — an ABSTAIN is a non-event, not a verdict.

    ABSTAIN is the engine's fail-closed answer for surface-disabled,
    no-credential, transport, malformed and empty-bundle runs
    (``engine/resolution.py``). A dead key is nothing to audit: recording it
    would make the history read as "the gate ran and decided".
    """
    from engine.resolution import VERDICT_ABSTAIN

    band = getattr(verdict, "verdict", None)
    if not band or band == VERDICT_ABSTAIN:
        return False
    return getattr(verdict, "abstain_reason", None) is None


def build_resolution_record(verdict, *, surface: str) -> dict:
    """The persisted payload for one resolution-gate verdict.

    A resolution record is NOT a task verdict: no ``passed`` (the gate returns a
    band, not a pass/fail), no criteria, no task id — and it says so with
    ``kind`` + ``source`` so no reader has to guess. The engine's own verdict
    dict ships whole under ``verdict`` (the object ``gitreins resolve --json``
    prints), so report/serve show the bundle manifest and the token and cost
    accounting without a second serialization that could drift from it.
    """
    return {
        "kind": KIND_RESOLUTION,
        "source": surface,
        "band": verdict.verdict,
        "probability": verdict.probability,
        "missing_kind": verdict.missing_kind,
        "question": verdict.question,
        # task_title is the label slot every existing reader already renders
        # (report rows, serve list rows, report --json summaries). For a
        # resolution record the one recognizable label is the question.
        "task_title": verdict.question,
        "verdict": verdict.to_dict(),
    }


def _token_count(value) -> int | None:
    """A reported token count as the schema stores it, else ``None``.

    The engine types both counts as ``int | None`` (a non-int usage value is
    dropped in ``ResolutionVerdict``), so anything else here means the response
    never reported a count — a bool included, which is an ``int`` in Python.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _append_resolution_usage(workdir: str, verdict, *, ts: float) -> bool:
    """Append the Jev call's real usage as one ``step: "resolution"`` row.

    Only a parsed, completed HTTP response reaches this point (every ABSTAIN
    returned from :func:`persist_resolution` before it), so the row carries what
    the API reported: ``input_tokens``/``output_tokens`` off the response's
    ``usage`` block. A response that reported no input tokens writes NO row
    rather than a 0/0 line — a fabricated zero would read as "the call was
    free" in the aggregates :mod:`engine.usage` feeds.

    *ts* is the record's own ``evaluated_at`` instant, passed in by the caller so
    the row and the record it belongs to share ONE measurement of the moment
    (see :func:`persist_resolution`).

    ``tokens_in`` already includes cache reads per the telemetry contract and
    this endpoint reports no cache counters, so both cache fields stay 0.
    """
    tokens_in = _token_count(getattr(verdict, "input_tokens", None))
    if not tokens_in:
        return False
    tokens_out = _token_count(getattr(verdict, "output_tokens", None))
    return usage.append_usage_row(
        workdir,
        step=RESOLUTION_USAGE_STEP,
        tokens_in=tokens_in,
        tokens_out=tokens_out or 0,
        ts=ts,
    )


def persist_resolution(workdir: str, verdict, *, surface: str) -> str:
    """Persist one resolution-gate verdict — the ONE path every surface uses.

    ``gitreins resolve``, ``gitreins preflight`` and the MCP ``context.resolve``
    tool all call this and never their own writer, so the record on disk is
    identical whichever surface ran the gate (the second-implementation drift
    that bit DF-GITREINS-POC-12 / -16, and the POC-23 lesson: an invisible
    verdict is not an audit trail).

    Returns the verdict commit hash, ``"dry-run"`` (files written, git
    unavailable), ``"disabled"`` (``history.enabled: false`` — NOTHING is
    written: no history entry AND no usage line), ``"abstain"`` (nothing
    written — a surface-disabled, no-credential, transport or parse failure is
    a non-event, not a verdict) or ``"error"``.

    One instant for both artifacts: the usage row and the record are written
    with the SAME timestamp, and the row goes first. Attribution in
    :func:`engine.usage.attribute_rows` is by time, so a row that merely
    *preceded* the record by a wall-clock read could still be charged to the
    next judge verdict when the two reads land in the same microsecond (a real
    case under load); stamping the row with the record's own ``evaluated_at``
    makes the record the earliest stamp at-or-after the row, deterministically.
    Write-first additionally means a failed persist leaves an unattributed row
    instead of a record with no telemetry.

    Non-fatal by contract, like :func:`persist_evaluation`: a persistence
    failure is logged and reported as ``"error"``, never raised into the run
    that produced the verdict. Never prints — the MCP server's stdout is its
    JSON-RPC channel.
    """
    if not _is_decision(verdict):
        return "abstain"
    try:
        persister = VerdictPersister(workdir)
        if not persister.enabled:
            return "disabled"
        stamp = datetime.utcnow()
        _append_resolution_usage(workdir, verdict, ts=_as_epoch(stamp))
        return persister.persist(
            RESOLUTION_ENTRY_ID,
            build_resolution_record(verdict, surface=surface),
            evaluated_at=stamp,
        )
    except Exception as exc:  # persistence must never fail the run
        logger.warning("Failed to persist resolution verdict (%s, non-fatal): %s", surface, exc)
        return "error"


# ── Report builder (shared between CLI and TUI) ────────────────


def build_report(workdir: str, n: int = 10) -> str:
    """Build a text report of recent verdicts.

    The history store holds two kinds of record (DF-GITREINS-POC-36). A
    RESOLUTION record is not a task verdict — it has no pass/fail — so it is
    listed in its own section and never counted in the pass/fail rollup: a
    RESOLVED band must not read as a passed judgment. A store with no
    resolution records renders exactly the report it always did.
    """
    persister = VerdictPersister(workdir)

    if not persister.enabled:
        return "History is disabled (history.enabled = false in config)."

    entries = persister.list_verdicts(n=n)
    if not entries:
        return "No verdict history found."

    resolution_entries = [e for e in entries if e.get("kind") == KIND_RESOLUTION]
    entries = [e for e in entries if e.get("kind") != KIND_RESOLUTION]

    total = len(entries)
    passed_count = sum(1 for e in entries if e.get("passed", False))
    fail_count = total - passed_count

    lines = []
    lines.append("═══ GitReins Verdict Report ═══")
    lines.append("")
    lines.append(f"Recent: {total} evaluations")
    lines.append(f"Pass:   {passed_count} ({_pct(passed_count, total)})")
    lines.append(f"Fail:   {fail_count} ({_pct(fail_count, total)})")
    lines.append("")

    for _i, entry in enumerate(entries):
        icon = "✓" if entry.get("passed") else "✗"
        task_id = entry.get("task_id", "?")
        date = entry.get("_date", "?")
        title = entry.get("task_title", task_id)
        verdict = entry.get("verdict", None)
        verdict_str = ""
        if verdict:
            verdict_str = verdict.verdict if hasattr(verdict, "verdict") else str(verdict)
            verdict_str = f" — {verdict_str}"

        items = entry.get("items", [])
        criteria_str = ""
        if items:
            item_statuses = []
            for item in items:
                if isinstance(item, dict):
                    s = "✓" if item.get("status") == "PASS" else "✗"
                else:
                    s = "✓" if getattr(item, "status", None) == "PASS" else "✗"
                item_statuses.append(s)
            criteria_str = f" [{''.join(item_statuses)}]"

        lines.append(f"  {icon} {task_id:<24} {date}  {criteria_str}{verdict_str}")
        if title and title != task_id:
            lines.append(f"     {title}")

    if resolution_entries:
        lines.append("")
        lines.append(f"─── Resolution gate ({len(resolution_entries)}) ───")
        lines.extend(_resolution_line(entry) for entry in resolution_entries)

    lines.append("")
    lines.append(f"Storage: {persister.storage_mode} ({persister.history_dir})")
    lines.append(f"Total entries: {persister.count_verdicts()}")

    return "\n".join(lines)


def _resolution_line(entry: dict) -> str:
    """One report line for a resolution record: band, probability, date, source.

    Reads only what the record itself carries (``band``/``probability`` →
    fallback to the nested engine verdict) so a record written by an older or
    partial writer still renders something true instead of an exception.
    """
    payload = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {}
    band = entry.get("band") or payload.get("verdict") or "RESOLUTION"
    probability = entry.get("probability")
    if probability is None:
        probability = payload.get("probability")
    shown = f" ({probability:.2f})" if isinstance(probability, (int, float)) else ""
    question = entry.get("task_title") or payload.get("question") or ""
    line = f"  • {band}{shown}  {entry.get('_date', '?')}  [{entry.get('source') or '?'}]"
    if question:
        line += f"\n      {question}"
    return line


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part * 100 // total}%"
