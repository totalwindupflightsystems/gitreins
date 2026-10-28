"""Read-only fingerprints of the final Git-visible workspace tree.

The authoritative digest is SHA-256 over this canonical byte layout::

    b"gitreins-working-tree-content-v1\\0"
    + repeated(sorted(path_bytes)) {
        uint64_be(len(path_bytes)) + path_bytes
        + uint64_be(len(content_bytes)) + content_bytes
      }

``path_bytes`` are Git's NUL-delimited repository-relative path bytes. Content is
what the workspace exposes after the index and working-tree overlays: regular-file
bytes or a symlink's target bytes. Missing index paths are deletions; untracked,
non-ignored files are included. File modes, HEAD ids, index blob ids, mtimes, and
absolute paths are deliberately absent. Thus staging or unstaging identical
workspace bytes cannot change the digest, and repositories with identical visible
content hash identically even when their HEADs differ (including an unborn HEAD).

This is an observation of a workspace, not an atomic snapshot. It can detect some
concurrent changes and otherwise fails closed where Git transformations make raw
workspace bytes non-authoritative, but it is not protection against an adversary
restoring previously observed state.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_AUTHORITATIVE = "authoritative"
_DOMAIN = b"gitreins-working-tree-content-v1\0"


@dataclass(frozen=True)
class FingerprintResult:
    """A workspace digest, dirty-path count, and authority state."""

    digest: str = ""
    dirty_count: int = 0
    state: str = _AUTHORITATIVE


class _Refusal(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


class _Deadline:
    def __init__(self, timeout_s: float) -> None:
        self._end = time.monotonic() + max(0.0, timeout_s)

    def remaining(self) -> float:
        remaining = self._end - time.monotonic()
        if remaining <= 0:
            raise _Refusal("timeout")
        return remaining

    def check(self) -> None:
        self.remaining()


def _git_env() -> dict[str, str]:
    """Keep caller config, but never let ambient routing select another repo/index."""
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    ):
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(
    root: Path,
    deadline: _Deadline,
    *args: str,
    input_bytes: bytes | None = None,
    ok: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            env=_git_env(),
            input=input_bytes,
            capture_output=True,
            timeout=deadline.remaining(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _Refusal("timeout") from exc
    except OSError as exc:
        raise _Refusal("git-error") from exc
    if proc.returncode not in set(ok):
        raise _Refusal("git-error")
    return proc


def _config_value(root: Path, deadline: _Deadline, key: str) -> str:
    proc = _git(root, deadline, "config", "--get", key, ok=(0, 1))
    return proc.stdout.decode("utf-8", "surrogateescape").strip() if proc.returncode == 0 else ""


def _truthy_git_value(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _config_drivers(root: Path, deadline: _Deadline, pattern: str) -> set[str]:
    proc = _git(root, deadline, "config", "--get-regexp", pattern, ok=(0, 1))
    drivers: set[str] = set()
    if proc.returncode != 0:
        return drivers
    for raw_line in proc.stdout.splitlines():
        key = raw_line.split(maxsplit=1)[0].decode("utf-8", "surrogateescape")
        parts = key.split(".")
        if len(parts) >= 3:
            drivers.add(".".join(parts[1:-1]))
    return drivers


def _parse_index(root: Path, deadline: _Deadline) -> dict[bytes, str]:
    proc = _git(root, deadline, "ls-files", "--stage", "-z")
    entries: dict[bytes, str] = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or not path:
            raise _Refusal("git-error")
        mode = fields[0].decode("ascii", "strict")
        stage = fields[2]
        if stage != b"0":
            raise _Refusal("unmerged-index")
        if mode == "160000":
            raise _Refusal("gitlink")
        entries[path] = mode
    return entries


def _untracked_paths(root: Path, deadline: _Deadline) -> list[bytes]:
    proc = _git(root, deadline, "ls-files", "--others", "--exclude-standard", "-z")
    return [path for path in proc.stdout.split(b"\0") if path]


def _flag_preflight(root: Path, deadline: _Deadline) -> None:
    object_format = _git(root, deadline, "rev-parse", "--show-object-format").stdout.strip()
    if object_format != b"sha1":
        raise _Refusal("non-sha1-object-format")

    if _truthy_git_value(_config_value(root, deadline, "core.autocrlf")):
        raise _Refusal("autocrlf")
    if _truthy_git_value(_config_value(root, deadline, "core.sparseCheckout")):
        raise _Refusal("sparse-checkout")
    if _truthy_git_value(_config_value(root, deadline, "core.sparseCheckoutCone")):
        raise _Refusal("sparse-checkout")

    shared_index = _git(root, deadline, "rev-parse", "--shared-index-path").stdout.strip()
    if shared_index or _truthy_git_value(_config_value(root, deadline, "core.splitIndex")):
        raise _Refusal("split-index")

    type_flags = _git(root, deadline, "ls-files", "-t", "-z").stdout.split(b"\0")
    for record in type_flags:
        if len(record) >= 3 and record[1:2] == b" " and record[:1] == b"S":
            raise _Refusal("skip-worktree")

    flags = _git(root, deadline, "ls-files", "-v", "-z").stdout.split(b"\0")
    for record in flags:
        if len(record) < 3 or record[1:2] != b" ":
            continue
        marker = record[:1]
        if marker.isalpha() and marker.islower():
            raise _Refusal("assume-unchanged")


def _attributes_preflight(root: Path, paths: list[bytes], deadline: _Deadline) -> None:
    if not paths:
        return
    proc = _git(
        root,
        deadline,
        "check-attr",
        "-z",
        "--stdin",
        "filter",
        "diff",
        "working-tree-encoding",
        input_bytes=b"\0".join(paths) + b"\0",
    )
    chunks = proc.stdout.split(b"\0")
    if chunks and chunks[-1] == b"":
        chunks.pop()
    if len(chunks) % 3:
        raise _Refusal("git-error")

    active_filters = _config_drivers(root, deadline, r"^filter\..*\.(clean|smudge|process)$")
    active_textconv = _config_drivers(root, deadline, r"^diff\..*\.textconv$")
    for index in range(0, len(chunks), 3):
        attribute = chunks[index + 1]
        raw_value = chunks[index + 2]
        if raw_value in {b"unspecified", b"unset"}:
            continue
        value = raw_value.decode("utf-8", "surrogateescape")
        if attribute == b"working-tree-encoding":
            raise _Refusal("working-tree-encoding")
        if attribute == b"filter" and value in active_filters:
            raise _Refusal("active-filter")
        if attribute == b"diff" and value in active_textconv:
            raise _Refusal("active-textconv")


def _has_nested_repo(root_bytes: bytes, relative: bytes) -> bool:
    relative = relative.rstrip(b"/")
    current = os.path.dirname(os.path.join(root_bytes, relative))
    while current and current != root_bytes:
        if os.path.lexists(os.path.join(current, b".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    candidate = os.path.join(root_bytes, relative)
    return (
        not os.path.islink(candidate)
        and os.path.isdir(candidate)
        and os.path.lexists(os.path.join(candidate, b".git"))
    )


def _read_visible_bytes(root_bytes: bytes, relative: bytes, deadline: _Deadline) -> bytes | None:
    deadline.check()
    if _has_nested_repo(root_bytes, relative):
        raise _Refusal("nested-repository")
    absolute = os.path.join(root_bytes, relative)
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Refusal("unreadable-path") from exc

    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(absolute)
        except OSError as exc:
            raise _Refusal("unreadable-path") from exc
        deadline.check()
        return target if isinstance(target, bytes) else os.fsencode(target)
    if stat.S_ISDIR(info.st_mode):
        if os.path.lexists(os.path.join(absolute, b".git")):
            raise _Refusal("nested-repository")
        return None
    if not stat.S_ISREG(info.st_mode):
        raise _Refusal("unreadable-path")

    digest_input = bytearray()
    try:
        with open(absolute, "rb") as handle:
            opened = os.fstat(handle.fileno())
            while True:
                deadline.check()
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest_input.extend(chunk)
            closed = os.fstat(handle.fileno())
    except OSError as exc:
        raise _Refusal("unreadable-path") from exc
    if (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) != (
        closed.st_dev,
        closed.st_ino,
        closed.st_size,
        closed.st_mtime_ns,
    ):
        raise _Refusal("workspace-changed")
    return bytes(digest_input)


def _dirty_count(root: Path, deadline: _Deadline) -> int:
    proc = _git(
        root,
        deadline,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    records = proc.stdout.split(b"\0")
    paths: set[bytes] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise _Refusal("git-error")
        status_code = record[:2]
        paths.add(record[3:])
        if b"R" in status_code or b"C" in status_code:
            if index >= len(records) or not records[index]:
                raise _Refusal("git-error")
            paths.add(records[index])
            index += 1
    return len(paths)


def compute(repo_root: str | os.PathLike[str], timeout_s: float = 30.0) -> FingerprintResult:
    """Observe *repo_root* without writing its checkout or real index.

    Every unsupported/non-authoritative condition returns a named ``state`` and
    an empty digest. Time spent in Git and file reads shares one deadline.
    """
    deadline = _Deadline(timeout_s)
    root = Path(repo_root).resolve()
    try:
        _flag_preflight(root, deadline)
        index_entries = _parse_index(root, deadline)
        untracked = _untracked_paths(root, deadline)
        paths = sorted(set(index_entries).union(untracked))
        _attributes_preflight(root, paths, deadline)

        root_bytes = os.fsencode(root)
        visible: dict[bytes, bytes] = {}
        for relative in paths:
            content = _read_visible_bytes(root_bytes, relative, deadline)
            if content is not None:
                visible[relative] = content

        digest = hashlib.sha256()
        digest.update(_DOMAIN)
        for relative in sorted(visible):
            content = visible[relative]
            digest.update(struct.pack(">Q", len(relative)))
            digest.update(relative)
            digest.update(struct.pack(">Q", len(content)))
            digest.update(content)
        dirty_count = _dirty_count(root, deadline)
        return FingerprintResult(digest.hexdigest(), dirty_count, _AUTHORITATIVE)
    except _Refusal as refusal:
        return FingerprintResult(state=refusal.state)
    except (OSError, UnicodeError, ValueError):
        return FingerprintResult(state="unreadable-path")
