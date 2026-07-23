"""
Antares CVE localization scanner — optional Tier 1 security guard.

This is a SCAFFOLD. The real ML-based CVE localization will be wired in
GR-117c once the Antares-1B model is confirmed available on HuggingFace.
For now, AntaresScanner runs a lightweight keyword-based heuristic that
flags files containing obvious security-related keywords.

Optional dependencies (installed later in GR-117c):
    huggingface_hub   — model download (snapshot_download)
    transformers      — tokenization + inference
    torch / onnxruntime — model runtime

Until those land, scan_file() returns zero-confidence findings marked
cve_id="CVE-SIMULATED" so guard integration can be exercised end-to-end
without requiring any ML stack to be installed.
"""

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("gitreins.antares")


# Default model location — matches the cache convention used elsewhere
# in engine/config.py (UPDATE_CACHE_DIR = ~/.cache/gitreins/).
_DEFAULT_CACHE_DIR = os.path.join(
    os.environ.get("HOME", os.path.expanduser("~")),
    ".cache", "gitreins",
)

# HuggingFace model id. Antares-1B is the FDTN-AI 1B-parameter model
# fine-tuned for code-level vulnerability localization.
_DEFAULT_MODEL_ID = "fdtn-ai/antares-1b"

# Fallback smaller variant. Kept as a config value rather than a constant
# so GR-117e can route between sizes via .gitreins/config.yaml.
_ALT_MODEL_ID = "fdtn-ai/antares-350m"

# Keywords that the heuristic scanner treats as a (very low-confidence)
# "CVE-SIMULATED" signal. Real ML inference replaces this in GR-117c.
_SIMULATED_KEYWORDS = (
    "CVE",
    "vulnerability",
    "injection",
    "exploit",
    "unsafe",
    "deserialization",
    "hardcoded",
)


@dataclass
class AntaresFinding:
    file: str
    line: int
    cve_id: str
    confidence: float  # 0.0 to 1.0
    description: str


class AntaresScanner:
    """Lazy-loading CVE localization scanner.

    The model is downloaded on first use into ~/.cache/gitreins/antares-1b/.
    Until transformers/onnxruntime are installed (GR-117c), the scanner
    falls back to a keyword-based heuristic that produces zero-confidence
    "CVE-SIMULATED" findings — useful for verifying the guard wiring
    end-to-end without pulling in a heavy ML stack.

    Args:
        workdir: Project root used for relative-path resolution and
            as the cwd for ``git diff`` when scanning staged files.
        model_id: HuggingFace model identifier. Defaults to Antares-1B.
        use_ml: When True, require huggingface_hub + transformers and
            attempt real ML inference (NOT YET IMPLEMENTED). When False
            (default), the keyword heuristic is used unconditionally —
            this is the only safe mode until GR-117c lands.
    """

    def __init__(
        self,
        workdir: str = ".",
        model_id: str = _DEFAULT_MODEL_ID,
        use_ml: bool = False,
    ):
        self.workdir = os.path.abspath(workdir)
        self.model_id = model_id
        self._use_ml = use_ml
        self._model_loaded = False
        self._local_model_path: str | None = None

    # ── Model loading (GR-117c will fill this in) ───────────────

    def _ensure_model(self) -> str | None:
        """Download the model if it isn't already on disk.

        Returns the local snapshot path, or None if huggingface_hub is
        unavailable and the model can't be resolved. Raises ImportError
        only when the caller has explicitly opted into ML mode.
        """
        if self._model_loaded:
            return self._local_model_path

        cache_dir = os.path.join(_DEFAULT_CACHE_DIR, "antares-1b")
        # If a previous snapshot exists, reuse it without contacting HF.
        if os.path.isdir(cache_dir) and os.listdir(cache_dir):
            self._local_model_path = cache_dir
            self._model_loaded = True
            logger.debug("Antares model cache hit: %s", cache_dir)
            return cache_dir

        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            msg = (
                "huggingface_hub is required to download the Antares model. "
                "Install with: pip install huggingface_hub"
            )
            if self._use_ml:
                raise ImportError(msg) from exc
            logger.debug("Antares model not loaded: %s", msg)
            return None

        try:
            local_path = snapshot_download(
                repo_id=self.model_id,
                cache_dir=_DEFAULT_CACHE_DIR,
            )
            self._local_model_path = local_path
            self._model_loaded = True
            logger.info("Antares model downloaded to %s", local_path)
            return local_path
        except Exception as exc:
            # Network errors, 404s, etc. — fall back to heuristic mode
            # unless the caller has explicitly required ML.
            msg = f"Failed to download Antares model ({self.model_id}): {exc}"
            if self._use_ml:
                raise RuntimeError(msg) from exc
            logger.warning(msg)
            return None

    # ── Public scanning API ──────────────────────────────────────

    def scan_file(self, filepath: str) -> list[AntaresFinding]:
        """Scan a single file for CVE-localized findings.

        SCAFFOLD BEHAVIOR (GR-117a): keyword heuristic. The file is
        read once; each line containing one of ``_SIMULATED_KEYWORDS``
        produces a single AntaresFinding with cve_id="CVE-SIMULATED"
        and confidence=0.0. Real ML inference replaces this in GR-117c.

        Args:
            filepath: Absolute or workdir-relative path to the file.

        Returns:
            A list of AntaresFinding objects (possibly empty).
        """
        findings: list[AntaresFinding] = []

        # Make absolute against the workdir when needed.
        if not os.path.isabs(filepath):
            filepath = os.path.join(self.workdir, filepath)

        try:
            with open(filepath, "r", errors="replace") as f:
                source = f.read()
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            logger.debug("Antares: cannot read %s: %s", filepath, exc)
            return findings

        rel = self._relpath(filepath)

        # Scaffold: keyword heuristic. One finding per matching line.
        # Real inference in GR-117c will replace this entire block.
        for lineno, line in enumerate(source.splitlines(), start=1):
            lowered = line.lower()
            for kw in _SIMULATED_KEYWORDS:
                if kw.lower() in lowered:
                    findings.append(AntaresFinding(
                        file=rel,
                        line=lineno,
                        cve_id="CVE-SIMULATED",
                        confidence=0.0,
                        description=(
                            f"Heuristic match on keyword '{kw}' — "
                            "real ML inference pending GR-117c"
                        ),
                    ))
                    # One finding per line keeps output readable.
                    break

        return findings

    def scan_staged_files(self) -> list[AntaresFinding]:
        """Scan every Python file in the git index (staged changes).

        Uses ``git diff --cached --name-only --diff-filter=ACM`` so
        only Added/Copied/Modified files are considered — deletions
        have no content to scan.
        """
        findings: list[AntaresFinding] = []
        staged = self._get_staged_files()
        for fpath in staged:
            if not fpath.endswith(".py"):
                continue
            full = os.path.join(self.workdir, fpath)
            if not os.path.isfile(full):
                continue
            findings.extend(self.scan_file(full))
        return findings

    def scan_directory(self, directory: str) -> list[AntaresFinding]:
        """Recursively scan every ``.py`` file under ``directory``."""
        findings: list[AntaresFinding] = []
        if not os.path.isabs(directory):
            directory = os.path.join(self.workdir, directory)
        if not os.path.isdir(directory):
            return findings

        # Skip the same noise dirs as engine.dead_code.DeadCodeDetector.
        skip_dirs = {
            ".git", "__pycache__", ".venv", "venv", "node_modules",
            ".tox", ".eggs", "build", "dist", ".pytest_cache",
            ".gitreins", "temporal-vector",
        }
        for root, dirs, filenames in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(root, fname)
                findings.extend(self.scan_file(full))
        return findings

    # ── Internals ────────────────────────────────────────────────

    def _relpath(self, abspath: str) -> str:
        try:
            return os.path.relpath(abspath, self.workdir)
        except ValueError:
            return abspath

    def _get_staged_files(self) -> list[str]:
        """Return staged file paths relative to the workdir."""
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                capture_output=True, text=True, timeout=10,
                cwd=self.workdir,
            )
            return [f.strip() for f in result.stdout.split("\n") if f.strip()]
        except Exception as exc:
            logger.debug("Antares: git diff failed: %s", exc)
            return []
