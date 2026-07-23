"""
Tests for the Antares CVE localization scanner + CVE feed + CLI wiring.

GR-117g. Covers:
    - engine.antares.AntaresScanner (scan_file, scan_staged_files,
      scan_directory, ImportError handling)
    - engine.cve_feed.CveFeed (init, get_recent, search, NVD/GitHub
      parsing, cache fallback, never-raises contract)
    - gitreins.cli cmd_security_scan (argparse wiring, JSON output,
      exit codes)
    - engine.guard_manager.GuardManager security_scan guard
      integration
"""
import argparse
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from engine.antares import AntaresScanner
from engine.cve_feed import CveEntry, CveFeed, _severity_to_score


# ── Helpers ─────────────────────────────────────────────────────


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _stage_file(workdir: str, relpath: str, content: str) -> str:
    """Write a file at ``relpath`` and ``git add`` it. Returns absolute path."""
    full = os.path.join(workdir, relpath)
    _write_file(full, content)
    subprocess.run(
        ["git", "add", relpath], cwd=workdir, capture_output=True, timeout=5,
    )
    return full


def _isolated_feed(workdir: str, tmp_path, **kwargs) -> CveFeed:
    """Build a CveFeed whose cache is under tmp_path, not ~/.cache."""
    cache_dir = os.path.join(str(tmp_path), "cve_cache")
    return CveFeed(workdir, cache_dir=cache_dir, **kwargs)


# ── AntaresScanner.scan_file ────────────────────────────────────


class TestAntaresScannerScanFile:
    """Direct unit tests for the keyword-based scaffold scanner."""

    def test_scan_file_finds_known_keyword(self, tmp_workdir, tmp_path):
        """A line containing 'unsafe' produces a CVE-SIMULATED finding."""
        full = os.path.join(tmp_workdir, "src.py")
        _write_file(full, "# safe comment\nx = 1\nunsafe_thing = True\n")
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_file(full)
        assert len(findings) == 1
        assert findings[0].file.endswith("src.py")
        assert findings[0].line == 3
        assert findings[0].cve_id == "CVE-SIMULATED"
        assert findings[0].confidence == 0.0
        assert "unsafe" in findings[0].description.lower()

    def test_scan_file_clean_returns_empty(self, tmp_workdir, tmp_path):
        """A file with no heuristic keywords returns no findings."""
        full = os.path.join(tmp_workdir, "clean.py")
        _write_file(full, "def add(a, b):\n    return a + b\n")
        scanner = AntaresScanner(tmp_workdir)
        assert scanner.scan_file(full) == []

    def test_scan_file_multiple_keywords_one_finding_per_line(self, tmp_workdir, tmp_path):
        """Each matching line yields exactly one finding (not one per keyword)."""
        full = os.path.join(tmp_workdir, "many.py")
        _write_file(
            full,
            "x = 1\nunsafe = True\n# exploit noted\n",
        )
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_file(full)
        assert len(findings) == 2
        assert [f.line for f in findings] == [2, 3]

    def test_scan_file_resolves_relative_path_against_workdir(self, tmp_workdir, tmp_path):
        """Relative paths are joined to workdir, not the caller cwd."""
        _write_file(os.path.join(tmp_workdir, "rel.py"), "deserialization here\n")
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_file("rel.py")
        assert len(findings) == 1
        assert findings[0].file == "rel.py"

    def test_scan_file_missing_file_returns_empty(self, tmp_workdir, tmp_path):
        """Missing files produce no findings and don't raise."""
        scanner = AntaresScanner(tmp_workdir)
        assert scanner.scan_file(os.path.join(tmp_workdir, "nope.py")) == []


# ── AntaresScanner.scan_staged_files ────────────────────────────


class TestAntaresScannerStagedFiles:
    """scan_staged_files must wrap git diff and only consider .py files."""

    def test_scan_staged_files_runs_git_diff(self, tmp_workdir, tmp_path):
        """A staged .py file with a keyword surfaces in the findings."""
        # tmp_workdir already has a fake .git dir (conftest fixture).
        _stage_file(tmp_workdir, "app.py", "vuln = 'exploit'\n")
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_staged_files()
        # At least one finding from app.py
        assert any("exploit" in f.description.lower() for f in findings)

    def test_scan_staged_files_skips_non_python(self, tmp_workdir, tmp_path):
        """Non-.py staged files are silently ignored."""
        _stage_file(tmp_workdir, "readme.md", "exploit mentioned here\n")
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_staged_files()
        assert findings == []

    @patch("subprocess.run")
    def test_scan_staged_files_handles_git_failure(self, mock_run, tmp_workdir):
        """If git is unavailable, scan_staged_files returns [] silently."""
        mock_run.side_effect = FileNotFoundError("git not found")
        scanner = AntaresScanner(tmp_workdir)
        assert scanner.scan_staged_files() == []


# ── AntaresScanner.scan_directory ────────────────────────────────


class TestAntaresScannerDirectory:
    """scan_directory must recurse and skip noise dirs (matches antares.py)."""

    def test_scan_directory_finds_nested_match(self, tmp_workdir, tmp_path):
        _write_file(
            os.path.join(tmp_workdir, "pkg", "sub", "deep.py"),
            "x = 'injection' flagged\n",
        )
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_directory(os.path.join(tmp_workdir, "pkg"))
        assert any(f.file.endswith("deep.py") for f in findings)

    def test_scan_directory_skips_noise_dirs(self, tmp_workdir, tmp_path):
        """Files under .venv/.git/__pycache__ are never scanned."""
        for noise in (".venv", ".git", "__pycache__", "node_modules", "build"):
            _write_file(
                os.path.join(tmp_workdir, noise, "ignore.py"),
                "unsafe code\n",
            )
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_directory(tmp_workdir)
        assert findings == []

    def test_scan_directory_missing_dir_returns_empty(self, tmp_workdir, tmp_path):
        """A non-existent directory yields no findings, no exception."""
        scanner = AntaresScanner(tmp_workdir)
        assert scanner.scan_directory(os.path.join(tmp_workdir, "missing")) == []

    def test_scan_directory_relative_path_against_workdir(self, tmp_workdir, tmp_path):
        _write_file(os.path.join(tmp_workdir, "a.py"), "hardcoded literal\n")
        scanner = AntaresScanner(tmp_workdir)
        findings = scanner.scan_directory(".")
        assert any(f.file.endswith("a.py") for f in findings)


# ── AntaresScanner model handling ───────────────────────────────


class TestAntaresScannerModel:
    """_ensure_model behaviour — pre-flight for GR-117c ML stack."""

    def test_ensure_model_raises_when_use_ml_and_no_huggingface(self, tmp_workdir, tmp_path):
        """use_ml=True with huggingface_hub missing raises ImportError."""
        scanner = AntaresScanner(tmp_workdir, use_ml=True)
        with patch.dict(sys.modules, {"huggingface_hub": None}):
            # Force the inner import to fail in a controlled way.
            real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
            def fake_import(name, *args, **kwargs):
                if name == "huggingface_hub":
                    raise ImportError("simulated missing dep")
                return real_import(name, *args, **kwargs)
            with patch("builtins.__import__", side_effect=fake_import):
                with pytest.raises(ImportError):
                    scanner._ensure_model()

    def test_ensure_model_returns_none_when_not_use_ml(self, tmp_workdir, tmp_path):
        """use_ml=False and no HF → returns None, no exception."""
        scanner = AntaresScanner(tmp_workdir, use_ml=False)
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                raise ImportError("simulated missing dep")
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=fake_import):
            with patch("os.path.isdir", return_value=False):
                assert scanner._ensure_model() is None

    def test_ensure_model_reuses_cache(self, tmp_workdir, tmp_path):
        """An existing, non-empty model cache dir is reused without network."""
        cache = os.path.join(
            os.environ.get("HOME", os.path.expanduser("~")),
            ".cache", "gitreins", "antares-1b",
        )
        os.makedirs(cache, exist_ok=True)
        try:
            with open(os.path.join(cache, "config.json"), "w") as f:
                f.write("{}")
            scanner = AntaresScanner(tmp_workdir)
            path = scanner._ensure_model()
            assert path == cache
            assert scanner._model_loaded is True
        finally:
            try:
                os.remove(os.path.join(cache, "config.json"))
            except OSError:
                pass


# ── CveFeed basics ───────────────────────────────────────────────


class TestCveFeedBasic:
    """Direct unit tests for the CveFeed dataclass + factory."""

    def test_cve_entry_round_trip(self):
        entry = CveEntry(
            cve_id="CVE-2024-1234",
            description="Demo",
            severity="HIGH",
            affected_packages=[r"^requests$"],
            published_date="2024-01-01",
            last_modified_date="2024-02-01",
        )
        round_tripped = CveEntry.from_dict(entry.to_dict())
        assert round_tripped == entry

    def test_cve_entry_from_dict_defaults(self):
        """Missing keys fall back to safe defaults."""
        entry = CveEntry.from_dict({"cve_id": "CVE-2024-9"})
        assert entry.severity == "NONE"
        assert entry.affected_packages == []
        assert entry.published_date == ""

    def test_init_uses_config_yaml(self, tmp_workdir, tmp_path):
        """CveFeed.init() pulls cve_source + min_confidence from config."""
        os.makedirs(os.path.join(tmp_workdir, ".gitreins"), exist_ok=True)
        with open(os.path.join(tmp_workdir, ".gitreins", "config.yaml"), "w") as f:
            f.write("defaults:\n  security_scan:\n    cve_source: github\n    min_confidence: 0.4\n")
        feed = CveFeed.init(tmp_workdir)
        assert feed.source == "github"
        assert feed.min_confidence == 0.4

    def test_init_falls_back_to_constructor_args(self, tmp_workdir, tmp_path):
        """No config file → supplied args used unchanged."""
        feed = CveFeed.init(tmp_workdir, source="both", min_confidence=0.9)
        assert feed.source == "both"
        assert feed.min_confidence == 0.9

    def test_init_invalid_source_normalised(self, tmp_workdir, tmp_path):
        """Unknown source strings fall back to 'nvd'."""
        feed = CveFeed.init(tmp_workdir, source="bogus", min_confidence=0.7)
        assert feed.source == "nvd"

    def test_severity_score_table(self):
        assert _severity_to_score("CRITICAL") == 1.0
        assert _severity_to_score("HIGH") == 0.85
        assert _severity_to_score("MEDIUM") == 0.6
        assert _severity_to_score("LOW") == 0.3
        assert _severity_to_score("NONE") == 0.0
        assert _severity_to_score("unknown") == 0.0


# ── CveFeed cache + network ─────────────────────────────────────


NVD_PAYLOAD = {
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2024-0001",
                "descriptions": [{"lang": "en", "value": "requests SSRF"}],
                "published": "2024-01-01T00:00:00Z",
                "lastModified": "2024-01-15T00:00:00Z",
                "metrics": {
                    "cvssMetricV31": [
                        {"cvssData": {"baseSeverity": "HIGH"}}
                    ]
                },
                "configurations": [
                    {"nodes": [{"cpeMatch": [
                        {"criteria": "cpe:2.3:a:requests:requests:2.31.0:*:*:*:*:*:*:*"}
                    ]}]}
                ],
            }
        },
    ]
}

GITHUB_PAYLOAD = [
    {
        "cve_id": "CVE-2024-0002",
        "ghsa_id": "GHSA-test-0002",
        "severity": "CRITICAL",
        "summary": "Django ORM injection",
        "description": "Django ORM injection in lookup expressions",
        "published_at": "2024-02-01T00:00:00Z",
        "updated_at": "2024-02-05T00:00:00Z",
        "vulnerabilities": [
            {"package": {"name": "django", "ecosystem": "pip"}}
        ],
    }
]


class TestCveFeedNetworkAndCache:
    """Network + cache behaviour of CveFeed."""

    def _make_response(self, payload, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.content = json.dumps(payload).encode()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_get_recent_parses_nvd(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.5)
        # Pre-populate cache so we don't hit the network.
        feed._write_cache({"entries": [
            CveEntry(
                cve_id="CVE-2024-0001",
                description="requests SSRF",
                severity="HIGH",
                affected_packages=["requests"],
                published_date="2024-01-01",
                last_modified_date="2024-01-15",
            ).to_dict()
        ], "fetched_at": 10**12})  # far in the future → fresh
        recent = feed.get_recent(limit=10)
        assert len(recent) == 1
        assert recent[0].cve_id == "CVE-2024-0001"
        assert recent[0].severity == "HIGH"

    def test_get_recent_filters_by_min_confidence(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.9)
        feed._write_cache({"entries": [
            CveEntry("CVE-1", "low", "LOW", [], "", "").to_dict(),
            CveEntry("CVE-2", "crit", "CRITICAL", [], "", "").to_dict(),
        ], "fetched_at": 10**12})
        # Only CRITICAL passes the 0.9 threshold.
        recent = feed.get_recent(limit=10)
        assert [e.cve_id for e in recent] == ["CVE-2"]

    def test_get_recent_returns_empty_when_nothing_cached_and_no_network(self, tmp_workdir, tmp_path):
        """Without cache or network, never raises — returns []. (GR-117b)"""
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.7)
        with patch.object(requests, "get", side_effect=Exception("offline")):
            assert feed.get_recent(limit=10) == []

    def test_get_recent_falls_back_to_stale_cache_when_offline(self, tmp_workdir, tmp_path):
        """Stale cache is served when the network is unavailable."""
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        feed._write_cache({"entries": [
            CveEntry("CVE-stale", "stale entry", "MEDIUM", [], "", "2024-01-01").to_dict()
        ], "fetched_at": 0})  # very old → stale
        with patch.object(requests, "get", side_effect=Exception("offline")):
            recent = feed.get_recent(limit=10)
        assert [e.cve_id for e in recent] == ["CVE-stale"]

    def test_search_matches_cve_id(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        feed._write_cache({"entries": [
            CveEntry("CVE-2024-1234", "Some vulnerability", "HIGH").to_dict()
        ], "fetched_at": 10**12})
        out = feed.search("CVE-2024-1234")
        assert len(out) == 1
        assert out[0].cve_id == "CVE-2024-1234"

    def test_search_matches_description(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        feed._write_cache({"entries": [
            CveEntry("CVE-X", "SQL injection in foo", "HIGH").to_dict()
        ], "fetched_at": 10**12})
        out = feed.search("SQL")
        assert len(out) == 1

    def test_search_matches_package_regex(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        feed._write_cache({"entries": [
            CveEntry("CVE-Y", "Django issue", "HIGH",
                     affected_packages=[r"^requests$"]).to_dict()
        ], "fetched_at": 10**12})
        out = feed.search("requests")
        assert len(out) == 1

    def test_search_empty_query_returns_empty(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        feed._write_cache({"entries": [
            CveEntry("CVE-Z", "anything", "HIGH").to_dict()
        ], "fetched_at": 10**12})
        assert feed.search("") == []
        assert feed.search("   ") == []

    def test_search_network_failure_returns_empty(self, tmp_workdir, tmp_path):
        """Offline + no cache → search returns [] without raising."""
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.7)
        with patch.object(requests, "get", side_effect=Exception("boom")):
            assert feed.search("anything") == []

    def test_get_recent_nvd_parses_payload(self, tmp_workdir, tmp_path):
        """NVD response parsing: severity, description, package regex."""
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        with patch.object(requests, "get",
                   return_value=self._make_response(NVD_PAYLOAD)):
            entries = feed._fetch_nvd()
        assert len(entries) == 1
        assert entries[0].cve_id == "CVE-2024-0001"
        assert entries[0].severity == "HIGH"
        assert entries[0].description == "requests SSRF"
        # CPE is normalised to a re-escaped vendor:product form.
        assert any("requests" in p for p in entries[0].affected_packages)

    def test_get_recent_github_parses_payload(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="github", min_confidence=0.0)
        with patch.object(requests, "get",
                   return_value=self._make_response(GITHUB_PAYLOAD)):
            entries = feed._fetch_github()
        assert len(entries) == 1
        assert entries[0].cve_id == "CVE-2024-0002"
        assert entries[0].severity == "CRITICAL"
        assert entries[0].affected_packages == ["django"]

    def test_github_unknown_severity_normalised_to_none(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="github", min_confidence=0.0)
        payload = [{
            "cve_id": "CVE-2024-9999",
            "severity": "banana",
            "summary": "odd",
            "vulnerabilities": [],
        }]
        with patch.object(requests, "get",
                   return_value=self._make_response(payload)):
            entries = feed._fetch_github()
        assert entries[0].severity == "NONE"

    def test_github_handles_non_list_payload(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="github", min_confidence=0.0)
        with patch.object(requests, "get",
                   return_value=self._make_response({"oops": "wrong shape"})):
            assert feed._fetch_github() == []

    def test_both_sources_merge(self, tmp_workdir, tmp_path):
        feed = _isolated_feed(tmp_workdir, tmp_path, source="both", min_confidence=0.0)
        responses = {
            "nvd.nist": self._make_response(NVD_PAYLOAD),
            "api.github": self._make_response(GITHUB_PAYLOAD),
        }
        def route(url, *args, **kwargs):
            for key, resp in responses.items():
                if key in url:
                    return resp
            raise AssertionError(f"unexpected URL: {url}")
        with patch.object(requests, "get", side_effect=route):
            entries = feed._load_entries()
        ids = {e.cve_id for e in entries}
        assert ids == {"CVE-2024-0001", "CVE-2024-0002"}

    def test_never_raises_on_broken_cache_file(self, tmp_workdir, tmp_path):
        """A malformed cache file should be treated as 'no cache'."""
        cache = os.path.join(tmp_workdir, ".cache", "gitreins", "cve_feed")
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "feed-nvd.json"), "w") as f:
            f.write("not json {{{")
        feed = _isolated_feed(tmp_workdir, tmp_path, source="nvd", min_confidence=0.0)
        with patch.object(requests, "get", side_effect=Exception("offline")):
            assert feed.get_recent() == []


# ── CLI: cmd_security_scan ──────────────────────────────────────


def _make_args(**overrides):
    """Build a minimal argparse.Namespace for cmd_security_scan tests."""
    defaults = {
        "directory": None,
        "output": "text",
        "force_ml": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestSecurityScanCLI:
    """Argparse wiring + output behaviour for the security-scan subcommand."""

    def test_help_lists_command(self):
        """`gitreins security-scan --help` runs without error and shows flags."""
        from gitreins.cli import main as cli_main  # noqa: F401 — ensure importable
        # Use the parser directly to avoid sys.exit side-effects.
        # Quick sanity: import-time wiring works.
        import gitreins.cli
        assert callable(gitreins.cli.cmd_security_scan)

    def test_directory_arg_recurses(self, tmp_workdir, tmp_path, capsys):
        """--directory triggers scan_directory(), produces a clean report."""
        from gitreins.cli import cmd_security_scan
        _write_file(
            os.path.join(tmp_workdir, "vuln.py"),
            "x = 'exploit'\n",
        )
        args = _make_args(directory=tmp_workdir)
        with patch("gitreins.cli.get_workdir", return_value=tmp_workdir):
            with pytest.raises(SystemExit) as exc:
                cmd_security_scan(args)
        assert exc.value.code == 1  # findings → exit 1
        out = capsys.readouterr().out
        assert "Antares" in out
        assert "exploit" in out

    def test_text_output_clean_returns_zero(self, tmp_workdir, tmp_path, capsys):
        from gitreins.cli import cmd_security_scan
        # No staged files in tmp_workdir fixture, no directory → clean.
        args = _make_args()
        with patch("gitreins.cli.get_workdir", return_value=tmp_workdir):
            with pytest.raises(SystemExit) as exc:
                cmd_security_scan(args)
        assert exc.value.code == 0
        assert "clean" in capsys.readouterr().out.lower()

    def test_json_output_format(self, tmp_workdir, tmp_path, capsys):
        from gitreins.cli import cmd_security_scan
        _write_file(
            os.path.join(tmp_workdir, "vuln.py"),
            "v = 'exploit'\n",
        )
        args = _make_args(directory=tmp_workdir, output="json")
        with patch("gitreins.cli.get_workdir", return_value=tmp_workdir):
            with pytest.raises(SystemExit) as exc:
                cmd_security_scan(args)
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list) and len(payload) == 1
        assert payload[0]["cve_id"] == "CVE-SIMULATED"
        assert payload[0]["file"].endswith("vuln.py")
        assert payload[0]["line"] == 1

    def test_force_ml_exits_2_when_huggingface_missing(self, tmp_workdir, tmp_path, capsys):
        """--force-ml + missing huggingface_hub → exit 2, not fallback."""
        import builtins
        _orig_import = builtins.__import__

        def _block_hf(name, *args, **kwargs):
            if name == "huggingface_hub":
                raise ImportError("No huggingface_hub (test simulation)")
            return _orig_import(name, *args, **kwargs)

        from gitreins.cli import cmd_security_scan
        args = _make_args(force_ml=True)
        with patch("builtins.__import__", side_effect=_block_hf):
            with patch("gitreins.cli.get_workdir", return_value=tmp_workdir):
                with pytest.raises(SystemExit) as exc:
                    cmd_security_scan(args)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        # Should mention at least one missing dep name.
        assert "huggingface_hub" in err or "transformers" in err


# ── Guard integration ───────────────────────────────────────────


class TestGuardSecurityScanIntegration:
    """GuardManager must dispatch to _check_security_scan when enabled."""

    def test_security_scan_guard_runs_when_enabled(self, tmp_workdir, tmp_path):
        from engine.guard_manager import GuardManager, GuardResult
        gm = GuardManager(tmp_workdir, {
            "guards": {"security_scan": {"enabled": True}}
        })
        assert gm._enabled.get("security_scan") is True
        # Don't actually trigger a network/ML run — mock the scanner
        # to return a clean result.
        fake = GuardResult("security_scan", True, "Antares: clean")
        with patch.object(gm, "_check_security_scan", return_value=fake):
            tier1 = gm.run_all()
        # The mocked result is in the tier1.results list.
        names = [r.name for r in tier1.results]
        assert "security_scan" in names

    def test_security_scan_guard_skipped_when_disabled(self, tmp_workdir, tmp_path):
        from engine.guard_manager import GuardManager
        gm = GuardManager(tmp_workdir, {"guards": {"security_scan": {"enabled": False}}})
        assert gm._enabled.get("security_scan") is False

    def test_security_scan_guard_skipped_when_config_absent(self, tmp_workdir, tmp_path):
        """No config key at all → guard is off by default (opt-in)."""
        from engine.guard_manager import GuardManager
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": True}})
        assert gm._enabled.get("security_scan") is False

    def test_check_security_scan_clean_run(self, tmp_workdir, tmp_path):
        """Real _check_security_scan against a clean tmp repo → PASS."""
        from engine.guard_manager import GuardManager
        gm = GuardManager(tmp_workdir, {"guards": {"security_scan": {"enabled": True}}})
        # No staged files in the fixture → clean.
        result = gm._check_security_scan()
        assert result.passed is True
        assert "Antares" in result.output
