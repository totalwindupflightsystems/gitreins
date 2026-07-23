"""
Antares CVE localization scanner — optional Tier 1 security guard.

AntaresScanner supports two modes:
- opt-in local Antares-1B inference using HuggingFace Transformers and PyTorch,
  producing AntaresFinding records with real CVE identifiers and confidence
  scores emitted by the model.
- keyword fallback (the default), which needs no ML dependencies and produces
  zero-confidence ``CVE-SIMULATED`` findings on lines that contain known
  security-relevant keywords. The heuristic is preserved so the guard wiring
  can be exercised end-to-end without requiring the heavy ML stack.

Antares-1B is a causal language model trained for agentic vulnerability
localization. Its generated JSON findings are normalized into AntaresFinding
records. The model is downloaded lazily into the GitReins cache on first ML
scan, so importing and using the default scanner remains lightweight.
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

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
# fallback signal when ML inference is disabled or unavailable.
_SIMULATED_KEYWORDS = (
    "CVE",
    "vulnerability",
    "injection",
    "exploit",
    "unsafe",
    "deserialization",
    "hardcoded",
)

# Prompt and output limits keep a single scan bounded for local inference.
_MAX_SOURCE_CHARS = 32_000
_MAX_NEW_TOKENS = 512
# Antares-1B's positional context is 32k tokens; we tokenize in chunks of
# at most _CHUNK_TOKEN_BUDGET and reuse overlap between successive chunks
# so vulnerabilities that straddle a chunk boundary are still localized.
_CHUNK_TOKEN_BUDGET = 8_192
_CHUNK_OVERLAP_TOKENS = 256
_CVE_ID_PATTERN = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)


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
    Until transformers/onnxruntime are installed, or until ``use_ml=False``
    is requested, the scanner falls back to a keyword-based heuristic that
    produces zero-confidence ``CVE-SIMULATED`` findings — useful for
    verifying the guard wiring end-to-end without pulling in a heavy ML
    stack.

    Args:
        workdir: Project root used for relative-path resolution and
            as the cwd for ``git diff`` when scanning staged files.
        model_id: HuggingFace model identifier. Defaults to Antares-1B.
        use_ml: When True, load Antares-1B and run local Transformers/PyTorch
            inference. If model loading or inference fails, scan_file falls
            back to the keyword heuristic. When False (default), keyword mode
            is used without importing the optional ML stack.
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
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        # Context window in tokens — populated lazily once the tokenizer is
        # loaded so chunked inference can size its windows correctly.
        self._token_limit: int | None = None

    # ── Model loading ─────────────────────────────────────────────

    def _ensure_model(self) -> str | None:
        """Download the model if it isn't already on disk and load it.

        Returns the local snapshot path, or None if huggingface_hub is
        unavailable and the model can't be resolved. When ``use_ml`` is
        True, this method also loads the tokenizer + AutoModel into memory
        so subsequent ``_scan_with_model`` calls reuse the cached weights.

        Raises ImportError only when the caller has explicitly opted into
        ML mode and huggingface_hub isn't installed.
        """
        if self._model_loaded and self._local_model_path is not None:
            return self._local_model_path

        cache_dir = os.path.join(_DEFAULT_CACHE_DIR, "antares-1b")
        # If a previous snapshot exists, reuse it without contacting HF.
        if os.path.isdir(cache_dir) and os.listdir(cache_dir):
            self._local_model_path = cache_dir
        else:
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
                self._local_model_path = snapshot_download(
                    repo_id=self.model_id,
                    cache_dir=_DEFAULT_CACHE_DIR,
                )
                logger.info("Antares model downloaded to %s", self._local_model_path)
            except Exception as exc:
                # Network errors, 404s, etc. — fall back to heuristic mode
                # unless the caller has explicitly required ML.
                msg = f"Failed to download Antares model ({self.model_id}): {exc}"
                if self._use_ml:
                    raise RuntimeError(msg) from exc
                logger.warning(msg)
                return None

        # Load tokenizer + model weights when ML is enabled. Failures here
        # propagate so callers (or the scan_file fallback path) can react.
        if self._use_ml:
            self._load_model_objects(self._local_model_path)

        self._model_loaded = True
        return self._local_model_path

    def _load_model_objects(self, model_path: str) -> None:
        """Materialize the tokenizer and AutoModel once per scanner.

        On success ``self._tokenizer`` and ``self._model`` are populated.
        On missing deps, an ImportError is raised so the scan_file fallback
        can downgrade to the keyword heuristic.
        """
        if self._tokenizer is not None and self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise ImportError(
                "Antares ML inference requires transformers and torch. "
                "Install with: pip install transformers torch"
            ) from exc

        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model_path)
        self._model = AutoModelForCausalLM.from_pretrained(model_path)
        self._model.eval()
        # Best-effort: many causal LM tokenizers expose model_max_length.
        token_limit = getattr(self._tokenizer, "model_max_length", None)
        if isinstance(token_limit, int) and token_limit > 0:
            self._token_limit = token_limit
        else:
            self._token_limit = 32_768

    # ── ML inference path ─────────────────────────────────────────

    def _scan_with_model(self, source_code: str) -> list[AntaresFinding]:
        """Run the Antares model over the source and return findings.

        The source is split into overlapping token chunks so files larger
        than the model's context window still produce complete coverage.
        Each chunk yields its own JSON array of findings; chunk-local line
        numbers are translated back to the original file's line numbers.

        Failures (missing deps, broken cache, OOM, parsing errors) are
        re-raised so :meth:`scan_file` can downgrade to the heuristic.
        """
        model_path = self._ensure_model()
        if model_path is None:
            raise RuntimeError("Antares model is unavailable")

        tokenizer = self._tokenizer
        model = self._model
        torch = self._torch
        if tokenizer is None or model is None or torch is None:
            # _ensure_model should have populated these; defensive guard.
            raise RuntimeError("Antares model runtime was not initialized")

        findings: list[AntaresFinding] = []
        for chunk_start_line, chunk_text in self._chunk_source(source_code):
            chunk_findings = self._infer_chunk(chunk_text, chunk_start_line)
            findings.extend(chunk_findings)
        return findings

    # ── Heuristic fallback path ───────────────────────────────────

    def _scan_with_heuristic(self, filepath: str) -> list[AntaresFinding]:
        """Run the keyword-based heuristic against ``filepath``.

        Each line matching one of ``_SIMULATED_KEYWORDS`` produces one
        zero-confidence ``CVE-SIMULATED`` AntaresFinding. Used when
        ``use_ml`` is False or the ML stack can't be reached.
        """
        findings: list[AntaresFinding] = []
        rel = self._relpath(filepath)
        try:
            with open(filepath, "r", errors="replace") as f:
                source = f.read()
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            logger.debug("Antares: cannot read %s: %s", filepath, exc)
            return findings

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

    # ── Chunked inference helpers ─────────────────────────────────

    def _chunk_source(self, source_code: str) -> list[tuple[int, str]]:
        """Split source into overlapping numbered chunks for inference.

        Returns a list of ``(start_line_1_indexed, chunk_text)`` tuples.
        Lines that don't fit in the model context are scanned in fixed-size
        windows with a small overlap so vulnerabilities near a chunk
        boundary remain detectable.
        """
        lines = source_code[:_MAX_SOURCE_CHARS].splitlines()
        if not lines:
            return []

        tokenizer = self._tokenizer
        if tokenizer is None:
            # Defensive: chunk_source shouldn't be called without a tokenizer.
            return [(1, "\n".join(lines))]

        token_limit = self._token_limit or _CHUNK_TOKEN_BUDGET
        # Reserve room for the prompt scaffold + generated JSON output.
        usable_tokens = max(512, token_limit - 1_024)

        # Build per-line token lengths once so chunk boundaries respect
        # both token budget and line granularity.
        line_token_lens: list[int] = []
        for line in lines:
            ids = tokenizer.encode(line, add_special_tokens=False)
            line_token_lens.append(max(1, len(ids)))

        chunks: list[tuple[int, str]] = []
        start_idx = 0
        n_lines = len(lines)
        while start_idx < n_lines:
            used = 0
            end_idx = start_idx
            while end_idx < n_lines and (used + line_token_lens[end_idx]) <= usable_tokens:
                used += line_token_lens[end_idx]
                end_idx += 1
            if end_idx == start_idx:
                # A single line exceeded usable_tokens — emit it alone.
                end_idx = start_idx + 1
            chunk_text = "\n".join(lines[start_idx:end_idx])
            chunks.append((start_idx + 1, chunk_text))
            if end_idx >= n_lines:
                break
            # Step back so we keep an overlap window of source lines.
            overlap_tokens = 0
            next_start = end_idx
            while next_start > start_idx and overlap_tokens < _CHUNK_OVERLAP_TOKENS:
                next_start -= 1
                overlap_tokens += line_token_lens[next_start]
            start_idx = max(start_idx + 1, next_start)
        return chunks

    def _infer_chunk(self, chunk_text: str, start_line: int) -> list[AntaresFinding]:
        """Run the model on a single chunk and parse its JSON response."""
        tokenizer = self._tokenizer
        model = self._model
        torch = self._torch
        assert tokenizer is not None and model is not None and torch is not None

        numbered_source = "\n".join(
            f"{start_line + offset}: {line}"
            for offset, line in enumerate(chunk_text.splitlines())
        )
        prompt = (
            "You are a source-code vulnerability localization engine. Analyze "
            "the Python file below for exploitable security vulnerabilities. "
            "Return ONLY a JSON array. Each item must have exactly these fields: "
            "cve_id (a known CVE identifier or CVE-UNSPECIFIED), line (1-based "
            "source line), confidence (number from 0 to 1), and description "
            "(short evidence-based explanation). Return [] when no vulnerability "
            "is present. Do not include markdown or prose outside the JSON array.\n\n"
            "SOURCE FILE:\n"
            f"{numbered_source}"
        )

        inputs = tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=self._token_limit or 32_768,
        )
        with torch.no_grad():
            generated = model.generate(
                **inputs,
                max_new_tokens=_MAX_NEW_TOKENS,
                do_sample=False,
            )

        generated_tokens = generated[0] if hasattr(generated, "__getitem__") else generated
        response = tokenizer.decode(generated_tokens, skip_special_tokens=True)
        return [
            AntaresFinding(
                file="",
                line=item["line"],
                cve_id=item["cve_id"],
                confidence=item["confidence"],
                description=item["description"],
            )
            for item in self._parse_inference_response(response)
        ]

    @staticmethod
    def _parse_inference_response(response: str) -> list[dict[str, Any]]:
        """Extract and validate the JSON finding array emitted by the model."""
        decoder = json.JSONDecoder()
        payload: Any = None
        for match in re.finditer(r"[\[{]", response):
            try:
                payload, _ = decoder.raw_decode(response[match.start():])
                break
            except json.JSONDecodeError:
                continue

        if isinstance(payload, dict):
            for key in ("findings", "vulnerabilities", "results"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
            else:
                payload = [payload]
        if not isinstance(payload, list):
            logger.warning("Antares model returned no parseable JSON findings")
            return []

        findings: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                line = int(item.get("line", item.get("line_number", 0)) or 0)
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if line < 1 or not 0.0 <= confidence <= 1.0:
                continue
            cve_id = str(item.get("cve_id", item.get("cve", "CVE-UNSPECIFIED"))).strip()
            cve_match = _CVE_ID_PATTERN.search(cve_id)
            if cve_match:
                cve_id = cve_match.group(0).upper()
            elif not cve_id.upper().startswith("CVE-"):
                cve_id = "CVE-UNSPECIFIED"
            description = str(
                item.get("description", item.get("reason", "Model-localized vulnerability"))
            ).strip()
            if not cve_id or not description:
                continue
            findings.append({
                "cve_id": cve_id,
                "line": line,
                "confidence": confidence,
                "description": description,
            })
        return findings

    # ── Public scanning API ──────────────────────────────────────

    def scan_file(self, filepath: str) -> list[AntaresFinding]:
        """Scan a single file for CVE-localized findings.

        In ML mode the source is passed to Antares-1B and its structured
        localization output becomes AntaresFinding objects. When ML mode is
        disabled, or model loading/inference is unavailable, the scanner
        falls back to the keyword heuristic and produces one zero-confidence
        ``CVE-SIMULATED`` finding per matching line.

        Args:
            filepath: Absolute or workdir-relative path to the file.

        Returns:
            A list of AntaresFinding objects (possibly empty).
        """
        # Make absolute against the workdir when needed.
        if not os.path.isabs(filepath):
            filepath = os.path.join(self.workdir, filepath)

        # Read the file up front; both code paths need the contents.
        try:
            with open(filepath, "r", errors="replace") as f:
                source = f.read()
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            logger.debug("Antares: cannot read %s: %s", filepath, exc)
            return []

        if self._use_ml:
            try:
                ml_findings = self._scan_with_model(source)
            except (ImportError, RuntimeError, OSError, ValueError) as exc:
                logger.warning("Antares ML inference unavailable for %s: %s",
                               self._relpath(filepath), exc)
            else:
                rel = self._relpath(filepath)
                for finding in ml_findings:
                    finding.file = rel
                return ml_findings

        # Keyword fallback preserves the lightweight default guard behavior.
        return self._scan_with_heuristic(filepath)

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
