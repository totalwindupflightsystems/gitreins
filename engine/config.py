"""
GitReins Unified Configuration — single source of truth for all defaults.

Every other module (LLM, evaluator, pipeline, guards, task manager)
reads its defaults from here rather than hardcoding them.

Load order (later overrides earlier):
    1. Hardcoded defaults (this file)
    2. .gitreins/config.yaml in the target repo
    3. Explicit constructor parameters
"""

import logging
import os
import json
import time
from dataclasses import dataclass, field

logger = logging.getLogger("gitreins.config")


# ── Constants ─────────────────────────────────────────────────

UPDATE_CACHE_DIR = os.path.join(
    os.environ.get("HOME", os.path.expanduser("~")),
    ".cache", "gitreins"
)
UPDATE_CACHE_FILE = os.path.join(UPDATE_CACHE_DIR, "update-check.json")


# ── Defaults dataclass ────────────────────────────────────────

@dataclass
class GitReinsDefaults:
    """Every default GitReins needs, in one place.

    Create with no args to get production defaults, or use
    load_defaults() to layer in .gitreins/config.yaml.
    """

    # ── LLM ──
    model: str = "deepseek-v4-flash"

    # ── Evaluation caps ──
    max_iterations: float = 100.0       # -1 = unlimited
    max_seconds: float = -1.0           # -1 = unlimited
    max_input_tokens: int = 10_000_000  # 10M
    max_output_tokens: int = 1_000_000  # 1M
    tool_call_weight: float = 0.1
    compaction_threshold: float = 0.90  # compact when 90% of input budget used (10% remaining)
    code_context_budget: float = 0.70   # cap pre-loaded code context to 70% of input budget

    # ── Update checking ──
    check_for_updates: bool = True
    update_check_ttl_hours: float = 24.0  # re-check after this many hours

    # ── History / verdict persistence ──
    history_enabled: bool = True
    history_path: str = ".gitreins/history"
    history_storage: str = "git"         # "git" or "filesystem"
    history_max_verdicts: int = 1000

    # Metadata
    _source: str = field(default="(built-in defaults)", repr=False)

    def overlay(self, config_dict: dict | None) -> "GitReinsDefaults":
        """Return a new defaults object with config.yaml values overlaid.

        Only keys explicitly present in config replace the defaults.
        """
        if not config_dict:
            return self

        defaults = config_dict.get("defaults", {})

        # Deep copy numeric/string fields
        result = GitReinsDefaults(
            model=defaults.get("model", self.model),
            max_iterations=_coerce_float(defaults.get("max_iterations", self.max_iterations)),
            max_seconds=_coerce_seconds(defaults.get("max_time", self.max_seconds)),
            max_input_tokens=_coerce_tokens(
                defaults.get("max_input_tokens", self.max_input_tokens)
            ),
            max_output_tokens=_coerce_tokens(
                defaults.get("max_output_tokens", self.max_output_tokens)
            ),
            tool_call_weight=float(defaults.get(
                "tool_call_weight", self.tool_call_weight
            )),
            compaction_threshold=float(defaults.get(
                "compaction_threshold", self.compaction_threshold
            )),
            code_context_budget=float(defaults.get(
                "code_context_budget", self.code_context_budget
            )),
            check_for_updates=bool(defaults.get(
                "check_for_updates", self.check_for_updates
            )),
            update_check_ttl_hours=_coerce_float(defaults.get(
                "update_check_ttl", self.update_check_ttl_hours
            )),
            history_enabled=bool(defaults.get(
                "history_enabled", self.history_enabled
            )),
            history_path=str(defaults.get("history_path", self.history_path)),
            history_storage=str(defaults.get(
                "history_storage", self.history_storage
            )),
            history_max_verdicts=int(defaults.get(
                "history_max_verdicts", self.history_max_verdicts
            )),
            _source=".gitreins/config.yaml" if defaults else self._source,
        )

        return result

    def to_config_dict(self) -> dict:
        """Produce a dict suitable for writing to .gitreins/config.yaml."""
        return {
            "model": self.model,
            "max_iterations": (
                int(self.max_iterations)
                if self.max_iterations == int(self.max_iterations)
                else self.max_iterations
            ),
            "max_time": (
                _fmt_seconds(self.max_seconds)
                if self.max_seconds > 0 else None
            ),
            "max_input_tokens": (
                _fmt_tokens(self.max_input_tokens)
                if self.max_input_tokens > 0 else None
            ),
            "max_output_tokens": (
                _fmt_tokens(self.max_output_tokens)
                if self.max_output_tokens > 0 else None
            ),
            "tool_call_weight": self.tool_call_weight,
            "compaction_threshold": self.compaction_threshold,
            "code_context_budget": self.code_context_budget,
            "check_for_updates": self.check_for_updates,
            "update_check_ttl": f"{int(self.update_check_ttl_hours)}h",
        }


# ── Factory ───────────────────────────────────────────────────

def load_defaults(workdir: str | None = None) -> GitReinsDefaults:
    """Load defaults, overlaid with .gitreins/config.yaml if present.

    Args:
        workdir: Repo root. If None, returns built-in defaults only.

    Returns:
        GitReinsDefaults with config.yaml values overlaid.
    """
    base = GitReinsDefaults()

    if not workdir:
        return base

    import yaml
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            return base.overlay(config)
        except Exception:
            logger.debug("Failed to load %s, using built-in defaults", config_path)

    return base


def load_raw_config(workdir: str | None = None) -> dict:
    """Load .gitreins/config.yaml as a raw dict. Returns {} if not found."""
    if not workdir:
        return {}
    import yaml
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


# ── Update checker ────────────────────────────────────────────

def check_for_update(workdir: str | None = None, force: bool = False) -> str | None:
    """Check PyPI for a newer version of GitReins.

    Respects update_check_ttl — only re-checks after the TTL expires
    unless force=True.

    Args:
        workdir: Repo root for loading config defaults.
        force: Bypass the TTL cache and check immediately.

    Returns:
        A message string if an update is available, or None if current.
    """
    from engine.version import __version__ as current_version

    defaults = load_defaults(workdir)

    if not defaults.check_for_updates and not force:
        return None

    # Check cache
    os.makedirs(UPDATE_CACHE_DIR, exist_ok=True)
    cache = _read_cache()
    last_checked = cache.get("last_checked", 0)
    ttl_seconds = defaults.update_check_ttl_hours * 3600

    if not force and time.time() - last_checked < ttl_seconds:
        cached_version = cache.get("latest_version", "")
        if cached_version and _version_greater(cached_version, current_version):
            return f"Update available: v{cached_version} → {_pypi_url()}"
        return None

    # Fetch from PyPI
    latest = _fetch_latest_version()
    if not latest:
        _write_cache({"last_checked": time.time(), "latest_version": current_version})
        return None

    _write_cache({"last_checked": time.time(), "latest_version": latest})

    if _version_greater(latest, current_version):
        return f"Update available: {current_version} → {latest} — {_pypi_url()}"

    return None


# ── Internal helpers ──────────────────────────────────────────

def _read_cache() -> dict:
    try:
        with open(UPDATE_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _write_cache(data: dict) -> None:
    try:
        os.makedirs(UPDATE_CACHE_DIR, exist_ok=True)
        with open(UPDATE_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fetch_latest_version() -> str | None:
    """Fetch the latest version string from PyPI JSON API."""
    import requests
    try:
        resp = requests.get("https://pypi.org/pypi/gitreins/json", timeout=5)
        resp.raise_for_status()
        return resp.json()["info"]["version"]
    except Exception:
        logger.debug("Failed to fetch latest version from PyPI", exc_info=True)
        return None


def _version_greater(newer: str, current: str) -> bool:
    """Compare two semantic version strings. True if newer > current."""
    from packaging.version import parse as parse_version
    try:
        return parse_version(newer) > parse_version(current)
    except Exception:
        # Fallback: string comparison
        return newer != current


def _pypi_url() -> str:
    return "https://pypi.org/project/gitreins/"


# ── Type coercion helpers ─────────────────────────────────────

def _coerce_float(val) -> float:
    """Coerce a config value to float, handling strings like '100'."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            pass
    return float(val) if isinstance(val, (int, float)) else -1.0


def _coerce_seconds(val) -> float:
    """Coerce a time string like '30m' or '2h' to seconds."""
    import re
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        m = re.match(r'^(\d+\.?\d*)\s*(s|sec|secs|m|min|mins|h|hr|hrs)$', val.lower())
        if m:
            num = float(m.group(1))
            unit = m.group(2)
            if unit in ('s', 'sec', 'secs'):
                return num
            elif unit in ('m', 'min', 'mins'):
                return num * 60
            elif unit in ('h', 'hr', 'hrs'):
                return num * 3600
        # Try plain number
        try:
            return float(val)
        except ValueError:
            pass
    return -1.0


def _coerce_tokens(val) -> int:
    """Coerce a token string like '10M', '200k', or '500' to int."""
    import re
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        val = val.strip()
        m = re.match(r'^(\d+\.?\d*)(k|m|K|M)?$', val)
        if m:
            num = float(m.group(1))
            suffix = (m.group(2) or '').lower()
            if suffix == 'k':
                num *= 1_000
            elif suffix == 'm':
                num *= 1_000_000
            return int(num)
    return int(val) if isinstance(val, (int, float)) else -1


def _fmt_seconds(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m" if s % 60 == 0 else f"{s // 60}m{s % 60}s"
    else:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h" if m == 0 else f"{h}h{m}m"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)
