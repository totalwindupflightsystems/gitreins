"""Install manifest, and the disable/enable/uninstall lifecycle (GR-161).

``gitreins install`` used to write hooks and a config with no record of what it
created. Three consequences followed: a user's own pre-commit hook was silently
overwritten, there was no supported way to take a repo out of enforcement without
deleting state, and removal meant hand-deleting files with no idea which were the
harness's.

Ownership has to be recorded when the artifact is created — it cannot be
reconstructed later — so install writes ``.gitreins/install-manifest.json``
naming every artifact it owns, its content hash at install time, and the backup
path of anything it displaced.

Owner rulings (2026-11-05) encoded here:

* plain ``uninstall`` removes only the hook and the config;
* ``--purge`` additionally removes ``.gitreins/`` history — nothing deletes
  history without that explicit flag;
* ``disable`` disarms enforcement WITHOUT removing config or history, and is
  reversible with ``enable``.

Every mutating command is dry-run unless ``--apply`` is passed, and prints the
exact operations it would perform.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import time
from typing import Any, Iterable

MANIFEST_RELPATH = ".gitreins/install-manifest.json"
BACKUP_DIR_RELPATH = ".gitreins/backups"
MANAGED_MARKER = "# gitreins-managed"
SCHEMA_VERSION = 1
HOOK_NAMES = ("pre-commit", "pre-push")


@dataclasses.dataclass
class Artifact:
    """One file GitReins owns inside a repo."""

    path: str  # repo-relative
    kind: str  # "hook" | "config"
    sha256: str = ""
    backup: str | None = None  # repo-relative path of a displaced user file

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "kind": self.kind, "sha256": self.sha256}
        if self.backup:
            payload["backup"] = self.backup
        return payload


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(workdir: str) -> str:
    return os.path.join(workdir, MANIFEST_RELPATH)


def read_manifest(workdir: str) -> dict[str, Any]:
    """The recorded manifest, or an empty skeleton when none exists."""
    path = manifest_path(workdir)
    if not os.path.isfile(path):
        return {"schema_version": SCHEMA_VERSION, "artifacts": [], "disabled": False}
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "artifacts": [], "disabled": False}
    if not isinstance(data, dict):
        return {"schema_version": SCHEMA_VERSION, "artifacts": [], "disabled": False}
    data.setdefault("artifacts", [])
    data.setdefault("disabled", False)
    return data


def write_manifest(workdir: str, manifest: dict[str, Any]) -> str:
    path = manifest_path(workdir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def _hook_path(workdir: str, name: str) -> str:
    """Resolve the hook path the way install does, honouring core.hooksPath."""
    import subprocess

    override = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if override:
        base = override if os.path.isabs(override) else os.path.join(workdir, override)
        return os.path.join(base, name)
    return os.path.join(workdir, ".git", "hooks", name)


def is_managed_hook(path: str) -> bool:
    """True when the file exists and carries GitReins' ownership marker."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return False
    return MANAGED_MARKER in head


def backup_displaced(workdir: str, hook_path: str, name: str) -> str | None:
    """Move a pre-existing, non-GitReins hook aside so it is not lost.

    Returns the repo-relative backup path, or None when there was nothing to
    displace (no file, or a hook GitReins itself owns).
    """
    if not os.path.isfile(hook_path) or is_managed_hook(hook_path):
        return None
    backup_dir = os.path.join(workdir, BACKUP_DIR_RELPATH)
    os.makedirs(backup_dir, exist_ok=True)
    backup_abs = os.path.join(backup_dir, f"{name}.pre-gitreins")
    if os.path.exists(backup_abs):
        # Never overwrite an earlier displaced hook: the first one is the
        # user's original, and a second install must not bury it.
        backup_abs = os.path.join(backup_dir, f"{name}.pre-gitreins.{int(time.time())}")
    shutil.copy2(hook_path, backup_abs)
    return os.path.relpath(backup_abs, workdir)


def record_install(workdir: str, artifacts: Iterable[Artifact]) -> str:
    """Write the manifest describing what install owns.

    Installing again refreshes hashes but preserves any backup already recorded,
    so a second install cannot orphan the user's original hook.
    """
    manifest = read_manifest(workdir)
    previous = {a.get("path"): a for a in manifest.get("artifacts", []) if isinstance(a, dict)}
    fresh: list[dict[str, Any]] = []
    for artifact in artifacts:
        entry = artifact.to_dict()
        old = previous.get(artifact.path)
        if old and old.get("backup") and not entry.get("backup"):
            entry["backup"] = old["backup"]
        fresh.append(entry)
    manifest["artifacts"] = fresh
    manifest["disabled"] = False
    return write_manifest(workdir, manifest)


# ── lifecycle operations ────────────────────────────────────────────────────
# Each returns the list of human-readable operations. Callers decide whether to
# apply them (they already mutated the filesystem) or print them as a dry run.


def disable(workdir: str) -> list[str]:
    """Disarm enforcement without deleting config or history.

    A displaced user hook is restored (control goes back to its owner); a hook
    GitReins created is removed. Config and ``.gitreins/`` stay exactly where
    they are, so ``enable`` puts everything back.
    """
    manifest = read_manifest(workdir)
    if manifest.get("disabled"):
        return ["already disabled — nothing to do"]
    steps: list[str] = []
    for entry in manifest.get("artifacts", []):
        if entry.get("kind") != "hook":
            continue
        rel = entry["path"]
        hook_abs = os.path.join(workdir, rel)
        backup = entry.get("backup")
        if backup:
            backup_abs = os.path.join(workdir, backup)
            if os.path.isfile(backup_abs):
                shutil.copy2(backup_abs, hook_abs)
                os.chmod(hook_abs, 0o755)
                steps.append(f"restored your original hook: {rel} <- {backup}")
                continue
            steps.append(f"backup missing for {rel}; removing the gitreins hook instead")
        if os.path.isfile(hook_abs):
            os.remove(hook_abs)
            steps.append(f"removed hook: {rel}")
    manifest["disabled"] = True
    write_manifest(workdir, manifest)
    steps.append(
        "config and .gitreins/ history left in place (use `gitreins uninstall` to remove them)"
    )
    return steps


def enable(workdir: str, hooks: dict[str, str]) -> list[str]:
    """Re-arm enforcement by rewriting the managed hooks.

    ``hooks`` maps hook name -> body, exactly as install renders it, so this
    function never invents hook content.
    """
    manifest = read_manifest(workdir)
    steps: list[str] = []
    for name in HOOK_NAMES:
        body = hooks.get(name)
        if body is None:
            continue
        hook_abs = _hook_path(workdir, name)
        os.makedirs(os.path.dirname(hook_abs), exist_ok=True)
        with open(hook_abs, "w") as handle:
            handle.write(body)
        os.chmod(hook_abs, 0o755)
        rel = os.path.relpath(hook_abs, workdir)
        entry = next((a for a in manifest["artifacts"] if a.get("path") == rel), None)
        if entry is None:
            entry = Artifact(rel, "hook").to_dict()
            manifest["artifacts"].append(entry)
        entry["sha256"] = sha256_file(hook_abs)
        steps.append(f"re-armed hook: {rel}")
    manifest["disabled"] = False
    write_manifest(workdir, manifest)
    return steps


def uninstall(workdir: str, *, purge: bool = False, apply: bool = False) -> list[str]:
    """Remove what install created: hooks and config, plus history with --purge.

    A config the user has edited since install is KEPT and reported — deleting
    someone's settings is not part of "remove the harness". History is only ever
    removed under ``--purge``.
    """
    manifest = read_manifest(workdir)
    steps: list[str] = []
    prefix = "" if apply else "would "
    for entry in manifest.get("artifacts", []):
        rel = entry.get("path")
        kind = entry.get("kind")
        abs_path = os.path.join(workdir, rel)
        if kind == "hook":
            backup = entry.get("backup")
            if backup and os.path.isfile(os.path.join(workdir, backup)):
                if apply:
                    shutil.copy2(os.path.join(workdir, backup), abs_path)
                    os.chmod(abs_path, 0o755)
                steps.append(f"{prefix}restore your original hook: {rel} <- {backup}")
            elif os.path.isfile(abs_path):
                if apply:
                    os.remove(abs_path)
                steps.append(f"{prefix}remove hook: {rel}")
        elif kind == "config":
            if not os.path.isfile(abs_path):
                continue
            if entry.get("sha256") and sha256_file(abs_path) != entry["sha256"]:
                steps.append(f"{prefix}KEEP config (edited since install): {rel}")
                continue
            if apply:
                os.remove(abs_path)
            steps.append(f"{prefix}remove config: {rel}")
    gitreins_dir = os.path.join(workdir, ".gitreins")
    if purge and os.path.isdir(gitreins_dir):
        if apply:
            shutil.rmtree(gitreins_dir)
        steps.append(f"{prefix}purge .gitreins/ (config, history, backups, manifest)")
    elif os.path.isdir(gitreins_dir):
        steps.append("kept .gitreins/ history — pass --purge to remove it")
    if apply:
        remaining = [
            a.get("path")
            for a in manifest.get("artifacts", [])
            if os.path.isfile(os.path.join(workdir, a.get("path") or ""))
        ]
        if not purge and os.path.isfile(manifest_path(workdir)):
            manifest["artifacts"] = [
                a for a in manifest.get("artifacts", []) if a.get("path") in remaining
            ]
            write_manifest(workdir, manifest)
    return steps
