"""
CVE feed — pull CVE data from NVD and/or GitHub Advisory Database.

GR-117b. The feed is the *lookup table* the Antares scanner uses to
translate localized vulnerability findings into CVE identifiers and
severity scores. The scanner itself (engine/antares.py) runs ML /
heuristic inference; this module provides the curated reference data.

Design rules (mirrors engine/antares.py):
    - Never raise out of this module. Every public method returns a
      well-typed empty value (list) on failure.
    - Cache aggressively. Default TTL is 24h. Both NVD and GitHub rate
      limit, and a CVE scan that requires a network round-trip per
      commit is unworkable.
    - Match existing engine/config.py cache conventions
      (UPDATE_CACHE_DIR = ~/.cache/gitreins/).
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from typing import Any

logger = logging.getLogger("gitreins.cve_feed")


# ── Cache location ──────────────────────────────────────────────
# Reuses the same root as engine/config.py:UPDATE_CACHE_DIR so all
# per-user GitReins caches land under ~/.cache/gitreins/.

_DEFAULT_CACHE_DIR = os.path.join(
    os.environ.get("HOME", os.path.expanduser("~")),
    ".cache", "gitreins", "cve_feed",
)

# Default TTL matches the existing update-check default in config.py
# (24h). CVE data shifts slowly; per-minute refresh buys nothing.
DEFAULT_TTL_SECONDS: int = 24 * 60 * 60

# Per-request HTTP timeout. Short enough that a missing network
# doesn't hang the guard for the whole pre-commit window.
HTTP_TIMEOUT: float = 5.0

# Public API endpoints. GitHub Advisory DB is the faster, friendlier
# source; NVD is the official registry.
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
GITHUB_URL = "https://api.github.com/advisories"

# Severity buckets the dataclass exposes. Kept as a literal set so
# callers can validate without importing the strings.
_VALID_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"}


# ── Dataclass ───────────────────────────────────────────────────


@dataclass
class CveEntry:
    """A single CVE record, normalized across NVD and GitHub sources.

    Severity follows CVSS v3 qualitative bands (CRITICAL/HIGH/MEDIUM/
    LOW/NONE). affected_packages holds regex strings — the Antares
    scanner matches the imported module/import path against each
    pattern, so callers should write patterns tight enough to limit
    false positives (e.g. ``^django(<5\\.0)?$`` rather than ``django``).
    """

    cve_id: str
    description: str
    severity: str
    affected_packages: list[str] = field(default_factory=list)
    published_date: str = ""
    last_modified_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CveEntry":
        return cls(
            cve_id=str(data.get("cve_id", "")),
            description=str(data.get("description", "")),
            severity=str(data.get("severity", "NONE")).upper(),
            affected_packages=list(data.get("affected_packages", []) or []),
            published_date=str(data.get("published_date", "")),
            last_modified_date=str(data.get("last_modified_date", "")),
        )


# ── Public API ──────────────────────────────────────────────────


class CveFeed:
    """Cached CVE feed backed by NVD and/or GitHub Advisory Database.

    Typical lifecycle:
        feed = CveFeed.init(workdir, source="both", min_confidence=0.7)
        recent = feed.get_recent(limit=20)
        matches = feed.search("requests")

    All network/cache failures are swallowed: ``get_recent`` and
    ``search`` return ``[]`` rather than raising. The warning is
    logged at WARNING level so it surfaces in the pre-commit hook
    output without blocking.
    """

    def __init__(
        self,
        workdir: str,
        source: str = "nvd",
        min_confidence: float = 0.7,
        cache_dir: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self.workdir = os.path.abspath(workdir)
        self.source = (source or "nvd").lower()
        if self.source not in {"nvd", "github", "both"}:
            logger.warning("Unknown cve_source %r — falling back to 'nvd'", self.source)
            self.source = "nvd"
        self.min_confidence = float(min_confidence)
        self.ttl_seconds = int(ttl_seconds)
        self.cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def init(
        cls,
        workdir: str,
        source: str = "nvd",
        min_confidence: float = 0.7,
    ) -> "CveFeed":
        """Construct a feed honoring the workdir's .gitreins/config.yaml.

        Pulls ``security_scan.cve_source`` and
        ``security_scan.min_confidence`` from the existing config when
        available so CLI callers and the guard see the same behaviour.
        Missing keys keep the supplied defaults.
        """
        cfg: dict[str, Any] = {}
        try:
            import yaml  # local import — pyyaml is an optional dep
            config_path = os.path.join(workdir, ".gitreins", "config.yaml")
            if os.path.isfile(config_path):
                with open(config_path, "r") as f:
                    loaded = yaml.safe_load(f) or {}
                cfg = loaded.get("defaults", {}).get("security_scan", {}) or {}
        except Exception as exc:  # noqa: BLE001 — never block on config
            logger.debug("CveFeed: failed to read config: %s", exc)

        effective_source = cfg.get("cve_source", source)
        effective_conf = cfg.get("min_confidence", min_confidence)
        return cls(
            workdir=workdir,
            source=effective_source,
            min_confidence=effective_conf,
        )

    # ── Public lookup methods ──────────────────────────────────

    def get_recent(self, limit: int = 50) -> list[CveEntry]:
        """Return the most recent ``limit`` CVE entries, freshest first.

        Tries network → cache → empty. Filters by ``min_confidence``
        using a simple severity→score mapping (CRITICAL=1.0,
        HIGH=0.85, MEDIUM=0.6, LOW=0.3, NONE=0.0).
        """
        entries = self._load_entries()
        entries = [e for e in entries if self._passes_severity(e)]
        # Newest first; fall back to cve_id desc if timestamps missing.
        entries.sort(
            key=lambda e: (e.last_modified_date, e.published_date, e.cve_id),
            reverse=True,
        )
        return entries[: max(0, int(limit))]

    def search(self, query: str) -> list[CveEntry]:
        """Return entries whose cve_id, description, or packages match ``query``.

        Match is case-insensitive substring on cve_id and description,
        regex on each ``affected_packages`` entry. An empty query
        returns ``[]`` so callers don't accidentally enumerate the
        whole feed.
        """
        if not query or not query.strip():
            return []
        entries = self._load_entries()
        needle = query.strip().lower()
        out: list[CveEntry] = []
        for entry in entries:
            if not self._passes_severity(entry):
                continue
            if needle in entry.cve_id.lower():
                out.append(entry)
                continue
            if needle in entry.description.lower():
                out.append(entry)
                continue
            for pattern in entry.affected_packages:
                try:
                    if re.search(pattern, query, flags=re.IGNORECASE):
                        out.append(entry)
                        break
                except re.error:
                    # Defensive: bad regex in the cached entry should
                    # not break the whole search.
                    continue
        return out

    # ── Internals ──────────────────────────────────────────────

    def _passes_severity(self, entry: CveEntry) -> bool:
        score = _severity_to_score(entry.severity)
        return score >= self.min_confidence

    def _load_entries(self) -> list[CveEntry]:
        """Return merged entries, refreshing stale cache when needed."""
        cached = self._read_cache()
        cached_entries = (
            self._coerce_entries(cached.get("entries", [])) if cached else []
        )
        if cached and cached_entries and not self._cache_is_stale(cached):
            return cached_entries

        # Try to refresh from network. If anything fails, fall back to
        # whatever is on disk (empty list if nothing was ever cached).
        refreshed: list[CveEntry] = []
        try:
            if self.source in ("nvd", "both"):
                refreshed.extend(self._fetch_nvd())
            if self.source in ("github", "both"):
                refreshed.extend(self._fetch_github())
        except Exception as exc:  # noqa: BLE001 — never raise
            logger.warning("CVE feed refresh failed: %s", exc)

        if refreshed:
            payload_entries = [
                e.to_dict() if isinstance(e, CveEntry) else e
                for e in refreshed
            ]
            self._write_cache({"entries": payload_entries, "fetched_at": int(time.time())})
            return refreshed

        if cached_entries:
            logger.warning(
                "CVE feed network unavailable; serving %d stale entries from cache",
                len(cached_entries),
            )
            return cached_entries

        logger.warning("CVE feed unavailable and cache empty; returning []")
        return []

    def _read_cache(self) -> dict[str, Any] | None:
        path = self._cache_path()
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("CVE cache unreadable (%s): %s", path, exc)
            return None

    def _coerce_entries(self, raw: list[Any]) -> list[CveEntry]:
        """Best-effort conversion of cache entries to CveEntry objects.

        A cache written by an older GitReins version (or a hand-edited
        file) may have plain dicts instead of CveEntry instances. The
        conversion is permissive: anything that doesn't look like a
        CVE record is dropped rather than raising.
        """
        out: list[CveEntry] = []
        for item in raw:
            if isinstance(item, CveEntry):
                out.append(item)
                continue
            if isinstance(item, dict):
                cve_id = item.get("cve_id")
                if not cve_id:
                    continue
                out.append(CveEntry.from_dict(item))
            # else: ignore — unknown shape, don't crash the whole feed
        return out

    def _write_cache(self, payload: dict[str, Any]) -> None:
        path = self._cache_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.debug("CVE cache write failed (%s): %s", path, exc)

    def _cache_path(self) -> str:
        return os.path.join(self.cache_dir, f"feed-{self.source}.json")

    def _cache_is_stale(self, cached: dict[str, Any]) -> bool:
        fetched_at = int(cached.get("fetched_at", 0) or 0)
        if fetched_at <= 0:
            return True
        return (int(time.time()) - fetched_at) > self.ttl_seconds

    # ── Source: NVD ────────────────────────────────────────────

    def _fetch_nvd(self) -> list[CveEntry]:
        """Fetch recent CVEs from NVD's public REST API.

        NVD requires no authentication but rate-limits aggressively
        (~5 requests / 30s without an API key). The 5s HTTP timeout
        and the caller-side cache keep this from blocking pre-commit
        hooks during incidents.
        """
        try:
            import requests  # local import — matches project pattern
        except ImportError:
            logger.warning("requests not available — skipping NVD fetch")
            return []

        try:
            resp = requests.get(
                NVD_URL,
                params={"resultsPerPage": 50, "startIndex": 0},
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": "gitreins/0.10.2"},
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("NVD request failed: %s", exc)
            return []

        data = resp.json() if resp.content else {}
        items = data.get("vulnerabilities", []) if isinstance(data, dict) else []
        entries: list[CveEntry] = []
        for item in items:
            cve = item.get("cve", {}) if isinstance(item, dict) else {}
            cve_id = cve.get("id", "")
            if not cve_id:
                continue
            description = _extract_nvd_description(cve)
            severity = _extract_nvd_severity(cve)
            published = cve.get("published", "") or ""
            modified = cve.get("lastModified", "") or ""
            affected = _extract_nvd_packages(cve)
            entries.append(CveEntry(
                cve_id=cve_id,
                description=description,
                severity=severity,
                affected_packages=affected,
                published_date=published,
                last_modified_date=modified,
            ))
        return entries

    # ── Source: GitHub ─────────────────────────────────────────

    def _fetch_github(self) -> list[CveEntry]:
        """Fetch recent advisories from the GitHub Advisory Database."""
        try:
            import requests
        except ImportError:
            logger.warning("requests not available — skipping GitHub fetch")
            return []

        try:
            resp = requests.get(
                GITHUB_URL,
                params={"per_page": 50},
                timeout=HTTP_TIMEOUT,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "gitreins/0.10.2",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitHub advisory request failed: %s", exc)
            return []

        data = resp.json() if resp.content else []
        if not isinstance(data, list):
            return []

        entries: list[CveEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cve_id = item.get("cve_id") or item.get("ghsa_id") or ""
            if not cve_id:
                continue
            severity = str(item.get("severity", "NONE") or "NONE").upper()
            if severity not in _VALID_SEVERITIES:
                severity = "NONE"
            description = str(item.get("description", "") or item.get("summary", ""))
            published = str(item.get("published_at", "") or "")
            modified = str(item.get("updated_at", "") or "")
            affected = _extract_github_packages(item)
            entries.append(CveEntry(
                cve_id=cve_id,
                description=description,
                severity=severity,
                affected_packages=affected,
                published_date=published,
                last_modified_date=modified,
            ))
        return entries


# ── Helpers ──────────────────────────────────────────────────────


def _severity_to_score(severity: str) -> float:
    """Map CVSS qualitative band to a 0..1 confidence score."""
    return {
        "CRITICAL": 1.0,
        "HIGH": 0.85,
        "MEDIUM": 0.6,
        "LOW": 0.3,
        "NONE": 0.0,
    }.get(str(severity or "NONE").upper(), 0.0)


def _extract_nvd_description(cve: dict) -> str:
    """NVD stores descriptions in a per-locale array — pick English."""
    for desc in cve.get("descriptions", []) or []:
        if isinstance(desc, dict) and desc.get("lang") == "en":
            return str(desc.get("value", ""))
    return ""


def _extract_nvd_severity(cve: dict) -> str:
    """NVD nests severity under metrics.cvssMetricV3[].cvssData.baseSeverity."""
    for metric in cve.get("metrics", {}).get("cvssMetricV31", []) or []:
        data = metric.get("cvssData", {}) or {}
        sev = str(data.get("baseSeverity", "")).upper()
        if sev in _VALID_SEVERITIES:
            return sev
    for metric in cve.get("metrics", {}).get("cvssMetricV30", []) or []:
        data = metric.get("cvssData", {}) or {}
        sev = str(data.get("baseSeverity", "")).upper()
        if sev in _VALID_SEVERITIES:
            return sev
    for metric in cve.get("metrics", {}).get("cvssMetricV2", []) or []:
        data = metric.get("cvssData", {}) or {}
        sev = str(data.get("baseSeverity", "")).upper()
        if sev in _VALID_SEVERITIES:
            return sev
    return "NONE"


def _extract_nvd_packages(cve: dict) -> list[str]:
    """Convert CPE URNs to coarse package regex strings.

    NVD uses CPE 2.3 formatted URNs like
    ``cpe:2.3:a:requests:requests:2.31.0:*:*:*:*:*:*:*``. The 7-element
    prefix is ``cpe:2.3:<part>:<vendor>:<product>:<version>:<update>``
    so we split on ``:`` and pick positions 2-4 to recover the
    vendor+product pair. We emit ``<vendor>:<product>`` as an
    ``re.escape``d regex — enough to match common package names
    without dragging the full version matrix into a guard-time scan.
    """
    patterns: list[str] = []
    for config in cve.get("configurations", []) or []:
        for node in config.get("nodes", []) or []:
            for cpe in node.get("cpeMatch", []) or []:
                urn = cpe.get("criteria", "")
                parts = urn.split(":")
                # CPE 2.3: cpe:2.3:part:vendor:product:version:...
                if len(parts) >= 5 and parts[1] == "2.3" and parts[2] == "a":
                    vendor = parts[3]
                    product = parts[4]
                    if vendor and product and product != "*":
                        patterns.append(re.escape(f"{vendor}:{product}"))
    return patterns


def _extract_github_packages(item: dict) -> list[str]:
    """GitHub advisories list packages as ``{name, ecosystem}`` dicts."""
    out: list[str] = []
    for vuln in item.get("vulnerabilities", []) or []:
        if not isinstance(vuln, dict):
            continue
        pkg = vuln.get("package", {}) or {}
        name = pkg.get("name") if isinstance(pkg, dict) else None
        if name:
            out.append(re.escape(str(name)))
    return out
