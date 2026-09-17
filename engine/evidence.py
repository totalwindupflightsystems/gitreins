"""Worker execution evidence — bundle a verdict with the run that produced it.

A verdict records *what* was decided; the run that produced it (the brief the
worker was handed, the driver log it wrote, the patch the judge graded) usually
lives outside the repository — often in ``/tmp`` — and dies with the tick. This
module copies those artifacts next to ``verdict.json`` so one verdict directory
is a self-contained audit unit, and serves them back to the judgment viewer.

Sources (all optional; a verdict is never failed or delayed by missing
evidence — every failure is swallowed and reported as a missing item):

  brief  ``GITREINS_WORKER_BRIEF`` — path to the worker brief that was
         dispatched for the task, else ``<workdir>/.gitreins/worker-brief.md``
  log    ``GITREINS_DRIVER_LOG`` — path to the worker/driver log; only the tail
         is kept, because the interesting part of a driver log is its end
  patch  the diff the judge actually graded: ``git diff HEAD`` when the working
         tree is dirty, else the patch of the stamped commit (``git show``)

Everything is bounded: a brief keeps its head, a log keeps its tail, and the
patch is capped — each artifact records ``bytes``/``truncated`` so a reader can
tell a small artifact from a clipped one.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any

BRIEF_ENV = "GITREINS_WORKER_BRIEF"
LOG_ENV = "GITREINS_DRIVER_LOG"

BRIEF_NAME = "brief"
LOG_NAME = "log"
PATCH_NAME = "patch"

BRIEF_FILENAME = "worker-brief.md"
LOG_FILENAME = "driver-log.tail.txt"
PATCH_FILENAME = "commit.patch"

#: Character labels used by the viewer and the docs.
ITEM_LABELS = {
    BRIEF_NAME: "Worker brief",
    LOG_NAME: "Driver log (tail)",
    PATCH_NAME: "Graded patch",
}

#: Bounds. The patch is the largest artifact on purpose: a verdict for a
#: multi-file change is unreadable without it, and 256 KiB is still far below
#: what a single HTTP response can carry comfortably.
MAX_BRIEF_BYTES = 32 * 1024
MAX_LOG_BYTES = 16 * 1024
MAX_PATCH_BYTES = 256 * 1024

#: Marker written in place of the dropped head (tail reads) or tail (head
#: reads). It names the arithmetic so a reader never has to guess the loss.
DROP_MARKER = "... [{dropped} of {total} bytes dropped]\n"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _bounded_head(data: bytes, cap: int) -> tuple[str, bool]:
    """``(text, truncated)`` keeping the FIRST ``cap`` bytes of ``data``."""
    if len(data) <= cap:
        return _decode(data), False
    marker = DROP_MARKER.format(dropped=len(data) - cap, total=len(data))
    return marker + _decode(data[:cap]), True


def _bounded_tail(data: bytes, cap: int, total: int | None = None) -> tuple[str, bool]:
    """``(text, truncated)`` keeping the LAST ``cap`` bytes of a source.

    ``data`` is expected to already hold the source's tail (see
    :func:`_read_tail_bytes`) and ``total`` its full size, so the marker can
    name the dropped head without reading a multi-megabyte log into memory.
    """
    total = len(data) if total is None else total
    dropped = max(0, total - min(len(data), cap))
    if not dropped:
        return _decode(data), False
    marker = DROP_MARKER.format(dropped=dropped, total=total)
    return marker + _decode(data[len(data) - cap :]), True


def _read_bytes(path: str, cap: int) -> bytes | None:
    """First ``cap + 1`` bytes of ``path`` (so callers can see the overflow)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(cap + 1)
    except OSError:
        return None


def _read_tail_bytes(path: str, cap: int) -> tuple[bytes, int] | None:
    """``(tail, total_size)`` of ``path`` — the last ``cap`` bytes only."""
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as handle:
            if total > cap:
                handle.seek(total - cap)
            return handle.read(), total
    except OSError:
        return None


def _run_git(workdir: str, args: list[str], cap: int) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return _decode(result.stdout.encode("utf-8", errors="replace")[: cap + 1])


def _tree_is_dirty(workdir: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def graded_patch(workdir: str, commit: str = "") -> tuple[str, str]:
    """The diff the judge graded, as ``(text, source)`` — empty when git has none.

    A judge reads the working-tree diff first and falls back to the last commit
    when the tree is clean (``engine.evaluator``), so the evidence mirrors that
    order instead of always reporting the committed patch: a worker that lands
    its fix in the working tree is the normal case in this fleet.
    """
    if _tree_is_dirty(workdir):
        text = _run_git(workdir, ["diff", "HEAD", "--no-color"], MAX_PATCH_BYTES)
        if text:
            return text, "git diff HEAD (working tree)"
    if commit:
        text = _run_git(
            workdir,
            ["show", "--no-color", "--stat", "--patch", "--format=fuller", commit],
            MAX_PATCH_BYTES,
        )
        if text:
            return text, f"git show {commit[:12]}"
    text = _run_git(
        workdir,
        ["show", "--no-color", "--stat", "--patch", "--format=fuller", "HEAD"],
        MAX_PATCH_BYTES,
    )
    return text, "git show HEAD"


def _write_artifact(entry_dir: str, filename: str, text: str) -> int:
    path = os.path.join(entry_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def _item(
    name: str, filename: str, text: str, truncated: bool, source: str
) -> tuple[dict[str, Any], str]:
    """A manifest item plus the text to write for it."""
    return (
        {
            "name": name,
            "label": ITEM_LABELS[name],
            "file": filename,
            "bytes": len(text.encode("utf-8")),
            "truncated": truncated,
            "source": source,
        },
        text,
    )


def _collect_brief(workdir: str, env: dict[str, str]) -> tuple[dict[str, Any], str] | None:
    candidates: list[tuple[str, str]] = []
    env_path = env.get(BRIEF_ENV, "").strip()
    if env_path:
        candidates.append((env_path, BRIEF_ENV))
    candidates.append((os.path.join(workdir, ".gitreins", "worker-brief.md"), "worktree brief"))
    for path, source in candidates:
        data = _read_bytes(path, MAX_BRIEF_BYTES)
        if data is None:
            continue
        text, truncated = _bounded_head(data, MAX_BRIEF_BYTES)
        return _item(BRIEF_NAME, BRIEF_FILENAME, text, truncated, f"{source}: {path}")
    return None


def _collect_log(env: dict[str, str]) -> tuple[dict[str, Any], str] | None:
    path = env.get(LOG_ENV, "").strip()
    if not path:
        return None
    read = _read_tail_bytes(path, MAX_LOG_BYTES)
    if read is None:
        return None
    data, total = read
    text, truncated = _bounded_tail(data, MAX_LOG_BYTES, total=total)
    return _item(LOG_NAME, LOG_FILENAME, text, truncated, f"{LOG_ENV}: {path}")


def _collect_patch(workdir: str, commit: str) -> tuple[dict[str, Any], str] | None:
    text, source = graded_patch(workdir, commit)
    if not text.strip():
        return None
    text, truncated = _bounded_head(text.encode("utf-8"), MAX_PATCH_BYTES)
    return _item(PATCH_NAME, PATCH_FILENAME, text, truncated, source)


def collect_evidence(
    workdir: str,
    entry_dir: str,
    commit: str = "",
    task_id: str = "",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Copy the run's artifacts into ``entry_dir`` and return their manifest.

    Best-effort by contract: a source that is missing, unreadable or empty is
    left out of the manifest (the reader then sees "not recorded" instead of a
    fabricated artifact), and an unwritable ``entry_dir`` yields an empty
    manifest rather than an exception. ``task_id`` is recorded for provenance
    only — the artifacts carry whatever the source holds.
    """
    environ = dict(os.environ if env is None else env)
    items: list[dict[str, Any]] = []
    try:
        os.makedirs(entry_dir, exist_ok=True)
    except OSError:
        return {"collected_at": _now(), "items": [], "detail": "evidence directory is not writable"}

    for collector in (
        lambda: _collect_brief(workdir, environ),
        lambda: _collect_log(environ),
        lambda: _collect_patch(workdir, commit),
    ):
        try:
            collected = collector()
        except Exception:  # evidence must never break a verdict
            collected = None
        if not collected:
            continue
        item, text = collected
        try:
            _write_artifact(entry_dir, item["file"], text)
        except OSError:
            continue
        items.append(item)

    manifest: dict[str, Any] = {"collected_at": _now(), "items": items}
    if task_id:
        manifest["task_id"] = task_id
    return manifest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_items(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Manifest items of a verdict record, validated for serving.

    Legacy verdicts (recorded before evidence embedding) have no ``evidence``
    block and simply yield ``[]``.
    """
    evidence = verdict.get("evidence")
    if not isinstance(evidence, dict):
        return []
    raw_items = evidence.get("items")
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        filename = raw.get("file")
        if not isinstance(name, str) or not isinstance(filename, str):
            continue
        if not _safe_artifact_name(filename):
            continue
        items.append(raw)
    return items


def _safe_artifact_name(filename: str) -> bool:
    """A manifest filename must be a plain file name — never a path."""
    return bool(filename) and filename not in (".", "..") and os.path.basename(filename) == filename


def read_evidence(entry_dir: str, verdict: dict[str, Any], name: str) -> tuple[str, str] | None:
    """``(filename, text)`` for a manifest-declared artifact, else ``None``.

    The artifact must be declared in the verdict's own manifest AND be a plain
    file name, so a crafted ``name`` cannot walk out of the verdict directory;
    an unreadable artifact reads as ``None`` (a 404) rather than a 500.
    """
    for item in manifest_items(verdict):
        if item.get("name") != name:
            continue
        filename = item["file"]
        if not _safe_artifact_name(filename):
            return None
        path = os.path.join(entry_dir, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return filename, handle.read()
        except OSError:
            return None
    return None
