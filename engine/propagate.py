"""Propagator — copies guard config from the current GitReins repo to sibling repos.

All repos in a multi-repo project share the same quality gates.
Target overrides are always preserved during merge.
"""

import logging
import os
from copy import deepcopy

logger = logging.getLogger("gitreins.propagate")


class Propagator:
    """Copies .gitreins/config.yaml from source repo to target repos.

    When a target already has a config, the two are merged:
    source keys the target doesn't have are added; target's existing keys
    are preserved (target wins on conflicts).
    """

    def __init__(self, workdir: str):
        self.workdir = os.path.abspath(workdir)

    # ── Public API ──────────────────────────────────────────────

    def propagate(self, target_repos: list[str]) -> dict:
        """Propagate guard config to sibling repos.

        Args:
            target_repos: List of absolute or relative paths to target repos.

        Returns:
            Dict with ``source`` path and ``results`` list.
        """
        source_config = self._load_source_config()
        if source_config is None:
            return {
                "source": self.workdir,
                "error": f"No config found at "
                f"{os.path.join(self.workdir, '.gitreins', 'config.yaml')}",
            }

        results = []
        for target_raw in target_repos:
            target = os.path.abspath(target_raw)
            self._create_gitreins_dir(target)
            target_config_path = os.path.join(target, ".gitreins", "config.yaml")

            if os.path.isfile(target_config_path):
                result = self._merge_to_target(source_config, target_config_path)
            else:
                result = self._copy_to_target(source_config, target_config_path)

            results.append(result)

        return {"source": self.workdir, "results": results}

    # ── Config loading ──────────────────────────────────────────

    def _load_source_config(self) -> dict | None:
        """Read the source repo's .gitreins/config.yaml.

        Returns:
            The parsed config dict, or None if the file doesn't exist.
        """
        import yaml

        path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to load source config %s: %s", path, exc)
            return None

    # ── Directory scaffolding ───────────────────────────────────

    @staticmethod
    def _create_gitreins_dir(target: str) -> None:
        """Ensure ``.gitreins/`` exists under *target*."""
        gitreins_dir = os.path.join(target, ".gitreins")
        os.makedirs(gitreins_dir, exist_ok=True)

    # ── Override detection ─────────────────────────────────────

    @staticmethod
    def _should_override(key: str, target_config: dict) -> bool:
        """Return ``True`` if *key* should come from *target_config*.

        ``False`` means the key is absent in the target, so the source
        value should be used.
        """
        return key in target_config

    # ── Merge / copy helpers ────────────────────────────────────

    def _merge_to_target(
        self, source_config: dict, target_config_path: str
    ) -> dict:
        """Merge *source_config* into the existing config at *target_config_path*.

        Target values always win on key conflicts. Nested dicts are
        merged recursively.
        """
        import yaml

        with open(target_config_path) as f:
            target_config = yaml.safe_load(f) or {}

        merged, keys_added, keys_preserved = self._merge_dicts(
            source_config, target_config
        )

        with open(target_config_path, "w") as f:
            yaml.dump(merged, f, default_flow_style=False, sort_keys=False)

        return {
            "target": os.path.dirname(os.path.dirname(target_config_path)),
            "action": "merged",
            "keys_added": sorted(keys_added),
            "keys_preserved": sorted(keys_preserved),
        }

    def _copy_to_target(
        self, source_config: dict, target_config_path: str
    ) -> dict:
        """Write *source_config* wholesale to *target_config_path*."""
        import yaml

        with open(target_config_path, "w") as f:
            yaml.dump(source_config, f, default_flow_style=False, sort_keys=False)

        return {
            "target": os.path.dirname(os.path.dirname(target_config_path)),
            "action": "created",
            "keys_added": sorted(source_config.keys()),
            "keys_preserved": [],
        }

    # ── Recursive dict merge ────────────────────────────────────

    @staticmethod
    def _merge_dicts(
        source: dict, target: dict, prefix: str = ""
    ) -> tuple[dict, list[str], list[str]]:
        """Recursively merge *source* into *target*.

        Returns (merged_dict, keys_added, keys_preserved).

        *target* always wins on scalar conflicts.
        Nested dicts are merged key-by-key.
        """
        merged = deepcopy(target)
        keys_added: list[str] = []
        keys_preserved: list[str] = []

        for key, src_val in source.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if key not in target:
                # Source-only key — add it
                merged[key] = deepcopy(src_val)
                keys_added.append(full_key)
            else:
                tgt_val = target[key]
                if isinstance(src_val, dict) and isinstance(tgt_val, dict):
                    # Both are dicts — merge recursively
                    sub_merged, sub_added, sub_preserved = Propagator._merge_dicts(
                        src_val, tgt_val, prefix=full_key
                    )
                    merged[key] = sub_merged
                    keys_added.extend(sub_added)
                    keys_preserved.extend(sub_preserved)
                else:
                    # Target wins on scalar conflicts
                    keys_preserved.append(full_key)

        return merged, keys_added, keys_preserved
