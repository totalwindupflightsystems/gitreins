"""Canonical repository paths shared by GitReins board consumers.

Git worktrees share the main checkout's Git common directory but have separate
working trees.  The coding-hermes board is deliberately kept in the main
working tree, so callers must resolve it through Git rather than joining the
invoking worktree path.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeResolutionError(RuntimeError):
    """Raised when a canonical worktree board cannot be resolved safely."""


@dataclass(frozen=True)
class WorktreeIdentity:
    """Git identity and recorded branch point for the invoking checkout."""

    worktree_root: Path
    branch: str | None
    branch_point: str | None
    is_linked_worktree: bool


@dataclass(frozen=True)
class WorktreePaths:
    """Resolved paths for the invoking worktree and its shared board."""

    invoking_worktree_root: Path
    git_common_dir: Path
    canonical_main_root: Path
    canonical_board: Path
    local_board: Path

    @property
    def local_board_exists(self) -> bool:
        """Whether a separate in-tree board directory exists in this worktree."""
        return self.invoking_worktree_root != self.canonical_main_root and self.local_board.is_dir()


def _sanitized_git_env() -> dict[str, str]:
    """Keep inherited Git hook variables from selecting another checkout."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git_value(workdir: Path, *arguments: str) -> str:
    command = ["git", "-C", str(workdir), *arguments]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_sanitized_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeResolutionError(
            f"could not run {' '.join(command)!r} from {workdir}: {exc}"
        ) from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise WorktreeResolutionError(
            f"{workdir} is not inside a Git repository; {' '.join(command)} failed: {detail}"
        )

    value = result.stdout.strip()
    if not value or "\n" in value:
        raise WorktreeResolutionError(
            f"Git returned an invalid value for {' '.join(command)}: {value!r}"
        )
    return value


def _git_path(workdir: Path, value: str) -> Path:
    """Turn Git's possibly relative path output into an absolute path."""
    path = Path(value)
    if not path.is_absolute():
        path = workdir / path
    return path.resolve(strict=False)


def _git_optional_value(workdir: Path, *arguments: str) -> str | None:
    """Return a successful Git value, or ``None`` for an unavailable ref."""
    command = ["git", "-C", str(workdir), *arguments]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_sanitized_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeResolutionError(
            f"could not run {' '.join(command)!r} from {workdir}: {exc}"
        ) from exc
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value and "\n" not in value else None


def _registry_branch_point(main_root: Path, worktree_root: Path, branch: str | None) -> str | None:
    """Read a matching task branch point without treating the registry as truth."""
    registry = main_root / ".gitreins" / "worktrees.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    entries = data.get("worktrees", [])
    if not isinstance(entries, list):
        return None
    for item in entries:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not raw_path:
            continue
        candidate = Path(str(raw_path)).expanduser()
        if not candidate.is_absolute():
            candidate = main_root / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if candidate != worktree_root:
            continue
        if branch is not None and item.get("branch") not in (None, branch):
            continue
        point = item.get("branch_point")
        if isinstance(point, str) and point:
            return point
    return None


def resolve_worktree_identity(
    workdir: str | os.PathLike[str] | None = None,
) -> WorktreeIdentity:
    """Resolve the invoking checkout and its task branch merge base.

    A registered linked worktree uses the durable ``branch_point`` recorded by
    :class:`WorktreeManager`.  An unregistered linked worktree falls back to
    Git's merge-base against the canonical checkout's current branch.  Ordinary
    checkouts deliberately have no branch point so their diff-mode behavior
    remains staged-only; unborn repositories likewise have no merge base.
    """
    raw_workdir = Path.cwd() if workdir is None else Path(workdir).expanduser()
    invoking_dir = raw_workdir.resolve(strict=False)
    if not invoking_dir.is_dir():
        raise WorktreeResolutionError(f"worktree path is not a directory: {invoking_dir}")

    invoking_root = _git_path(
        invoking_dir,
        _git_value(invoking_dir, "rev-parse", "--show-toplevel"),
    )
    common_dir = _git_path(
        invoking_dir,
        _git_value(invoking_dir, "rev-parse", "--git-common-dir"),
    )
    if common_dir.name != ".git":
        raise WorktreeResolutionError(
            f"Git common directory has an unsupported layout: {common_dir}"
        )
    canonical_root = common_dir.parent
    branch = _git_optional_value(invoking_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    linked = invoking_root != canonical_root
    branch_point = None
    if linked:
        branch_point = _registry_branch_point(canonical_root, invoking_root, branch)
        if branch_point is None and branch:
            main_branch = _git_optional_value(
                canonical_root, "symbolic-ref", "--quiet", "--short", "HEAD"
            )
            if main_branch:
                result = subprocess.run(
                    ["git", "-C", str(invoking_dir), "merge-base", "HEAD", main_branch],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    env=_sanitized_git_env(),
                )
                if result.returncode == 0 and result.stdout.strip():
                    branch_point = result.stdout.strip()

    return WorktreeIdentity(
        worktree_root=invoking_root,
        branch=branch,
        branch_point=branch_point,
        is_linked_worktree=linked,
    )


def resolve_worktree_paths(workdir: str | os.PathLike[str] | None = None) -> WorktreePaths:
    """Resolve the invoking worktree and its canonical main-checkout board.

    Git's ``--git-common-dir`` is the source of truth for the shared Git
    directory.  Relative output is interpreted relative to the directory
    passed to ``git -C``.  A non-standard Git layout, bare repository, or
    missing board is rejected instead of falling back to a local worktree
    copy.
    """
    raw_workdir = Path.cwd() if workdir is None else Path(workdir).expanduser()
    invoking_dir = raw_workdir.resolve(strict=False)
    if not invoking_dir.is_dir():
        raise WorktreeResolutionError(f"worktree path is not a directory: {invoking_dir}")

    bare = _git_value(invoking_dir, "rev-parse", "--is-bare-repository")
    if bare == "true":
        raise WorktreeResolutionError(
            f"bare Git repositories have no checkout root: {invoking_dir}; "
            "run the command from a non-bare checkout"
        )
    if bare != "false":
        raise WorktreeResolutionError(
            f"Git returned unexpected bare-repository status {bare!r} for {invoking_dir}"
        )

    invoking_root = _git_path(
        invoking_dir,
        _git_value(invoking_dir, "rev-parse", "--show-toplevel"),
    )
    common_dir = _git_path(
        invoking_dir,
        _git_value(invoking_dir, "rev-parse", "--git-common-dir"),
    )
    if not common_dir.is_dir():
        raise WorktreeResolutionError(
            f"Git common directory does not exist or is not a directory: {common_dir}"
        )

    # A standard checkout's common dir is the .git directory directly beneath
    # the main checkout.  Requiring this relationship prevents an external or
    # malformed git-dir layout from making the board root ambiguous.
    canonical_root = common_dir.parent
    git_marker = canonical_root / ".git"
    if not git_marker.exists() or not git_marker.is_dir():
        raise WorktreeResolutionError(
            f"Git common directory {common_dir} does not belong to a standard main "
            f"checkout at {canonical_root}; expected {git_marker} to be a directory"
        )
    if git_marker.resolve(strict=False) != common_dir:
        raise WorktreeResolutionError(
            f"Git common directory is ambiguous: {common_dir} is not {git_marker}"
        )
    if not canonical_root.is_dir():
        raise WorktreeResolutionError(f"canonical main checkout does not exist: {canonical_root}")
    if invoking_root == canonical_root and not (canonical_root / ".git").is_dir():
        raise WorktreeResolutionError(
            f"invoking checkout has malformed Git metadata: {canonical_root / '.git'}"
        )

    board_candidate = canonical_root / ".coding-hermes" / "board"
    if not board_candidate.is_dir():
        raise WorktreeResolutionError(
            f"canonical board directory does not exist: {board_candidate}; "
            "create .coding-hermes/board in the main checkout"
        )
    canonical_board = board_candidate.resolve(strict=True)
    local_board = (invoking_root / ".coding-hermes" / "board").resolve(strict=False)

    return WorktreePaths(
        invoking_worktree_root=invoking_root,
        git_common_dir=common_dir,
        canonical_main_root=canonical_root,
        canonical_board=canonical_board,
        local_board=local_board,
    )


def canonical_board_dir(workdir: str | os.PathLike[str] | None = None) -> Path:
    """Return the validated shared board directory for ``workdir``."""
    return resolve_worktree_paths(workdir).canonical_board


def board_file_path(
    workdir: str | os.PathLike[str] | None,
    name: str,
) -> Path:
    """Return a validated file path inside the canonical board directory.

    This helper is shared by board readers and writers.  Only a direct child
    filename is accepted so a caller cannot escape the canonical store.
    """
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"board filename must be a direct child name: {name!r}")
    return canonical_board_dir(workdir) / name
