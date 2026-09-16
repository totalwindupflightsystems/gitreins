"""Language detection and per-language commands — one source of truth.

Three places in GitReins used to answer "what language is this repo?" with
three different rule sets, and they disagreed: ``gitreins init`` detected a
plain ``.py`` repo as Python while the judge's Tier 1 pipeline (signature
files only) graded that same repo *secrets only*.  A green judge verdict on
such a repo therefore meant nothing about the tree passing ``gitreins guard``.
This module owns the knowledge so the judge pipeline, the guard and ``init``
cannot drift apart again (DF-GITREINS-POC-16).

What lives here
---------------
``SIGNATURE_FILES``
    Ordered ``(marker, language)`` table.  The **first** match is the primary
    language.  Wildcards (``*.csproj``) are glob-expanded at the tree root.
    Order is part of the contract: existing consumers rely on e.g. ``go.mod``
    outranking ``package.json``.
``SOURCE_EXTENSIONS``
    ``extension -> language`` for the extension fallback (below).
``LANG_COMMANDS``
    ``language -> (lint_command, test_command)``.
``SKIP_DIRS`` / ``TEST_DIRS``
    Directory pruning rules for every scan in this module.

Detection rules (in order)
--------------------------
1. **Signature file** — an explicit ecosystem marker always wins.  A repo
   with ``pyproject.toml`` is Python even if most of its files are ``.ts``.
2. **Source-extension fallback** — when *no* signature file matches, scan the
   tree for source files and map extensions to languages.  Files are listed
   with ``git ls-files --cached --others --exclude-standard`` when *workdir*
   is a git repository (tracked + untracked-but-not-ignored, so freshly
   created files count and tool junk under a ``.gitignore`` does not); a
   bounded ``os.walk`` is used otherwise (not a repo, git missing, git error)
   or when git reports no files at all.
   The fallback requires at least ``MIN_SOURCE_FILES`` source file — one is
   enough, and it is deliberately low: the point is to stop grading an
   obviously-Python tree as if it were language-less.
3. **Nothing matches** — ``detect_language`` returns ``None``.  Callers must
   treat that as a *loud* degradation (see ``tier1_plan`` in
   ``engine/pipeline.py``), never as a silent secrets-only pass.

Scan pruning (applies to every walk in this module)
---------------------------------------------------
* Dot-directories are skipped wholesale (``.git``, ``.venv``, ``.gitreins``,
  ``.mypy_cache``, …) — they are tool/config state, not project source.
* ``SKIP_DIRS`` names vendored/build output (``node_modules``, ``dist``,
  ``build``, ``target``, ``site-packages``, ``vendor``, ``__pycache__``, …).
* ``TEST_DIRS`` components are excluded **from the fallback only**: a tree
  whose only sources live under ``tests/`` has no product code to lint or
  test, so it stays undetected.  A file at the tree *root* counts regardless
  of its name — a fresh repo whose single file is ``test_broken.py`` is still
  Python (that is exactly the DF-GITREINS-POC-16 repro).
* At most ``MAX_SCAN_FILES`` files are visited (bounded work on huge trees).
"""

import glob
import os
import subprocess

# ── Signature files ──────────────────────────────────────────────────────
# Ordered: first match wins. Wildcards are expanded at the tree root.
SIGNATURE_FILES: list[tuple[str, str]] = [
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("requirements.txt", "python"),
    ("Pipfile", "python"),
    ("package.json", "js"),
    ("tsconfig.json", "js"),
    ("pom.xml", "java"),
    ("settings.gradle.kts", "kotlin"),
    ("build.gradle", "java"),
    ("CMakeLists.txt", "cpp"),
    ("Makefile", "c"),
    ("Gemfile", "ruby"),
    ("*.gemspec", "ruby"),
    ("composer.json", "php"),
    ("*.csproj", "csharp"),
    ("*.sln", "csharp"),
    ("build.sbt", "scala"),
]

# ── Source extensions (fallback detection) ───────────────────────────────
# Insertion order is the tie-break for the fallback's "primary" language.
SOURCE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "js",
    ".tsx": "js",
    ".rb": "ruby",
    ".php": "php",
    ".java": "java",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}

# Source suffixes that make a tree C/C++ for the guard's cpp detection
# (derived from SOURCE_EXTENSIONS so the two can't drift).
CPP_SOURCE_SUFFIXES: tuple[str, ...] = tuple(
    ext for ext, lang in SOURCE_EXTENSIONS.items() if lang in ("c", "cpp")
)

# ── Per-language lint + test commands ────────────────────────────────────
# NOTE: no `2>/dev/null || true` suffix on any command here — appending one
# would zero the exit code and make a failing lint/test report as a pass
# (2026-08-08 fix; the pipeline's script step treats a non-zero exit as a
# hard failure regardless of `on_fail`).
LANG_COMMANDS: dict[str, tuple[str, str]] = {
    "go": ("go vet ./...", "go test ./..."),
    "rust": (
        "cargo clippy -- -D warnings",
        "cargo test --no-fail-fast",
    ),
    "python": (
        "ruff check . --quiet",
        "pytest -x --tb=short",
    ),
    "js": ("npx eslint .", "npm test"),
    "java": ("mvn checkstyle:check", "mvn test -q"),
    "c": ("make lint", "make test"),
    "cpp": ("make lint", "make test"),
    "ruby": ("rubocop", "bundle exec rspec"),
    "php": (
        "php vendor/bin/phpcs",
        "php vendor/bin/phpunit",
    ),
    "kotlin": ("./gradlew lint", "./gradlew test"),
    "csharp": (
        "dotnet format --verify-no-changes",
        "dotnet test",
    ),
    "scala": ("sbt scalafmtCheck", "sbt test"),
}

# ── Scan pruning ─────────────────────────────────────────────────────────
# Directories never scanned by any detection in this module. Dot-directories
# are skipped by prefix as well, so this table lists only the non-dotted ones.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "venv",
        "build",
        "dist",
        "target",
        "__pycache__",
        "site-packages",
        "dist-packages",
        "vendor",
        "htmlcov",
    }
)

# Directory components excluded from the EXTENSION FALLBACK only. A tree whose
# only sources are tests has nothing for lint/tests to grade, so it must not
# be reported as a detected language (DF-002 keeps its narrow semantics).
TEST_DIRS: frozenset[str] = frozenset(
    {"test", "tests", "testing", "testdata", "spec", "specs", "__tests__", "e2e"}
)

# Marker subset for "does this repo have Python packaging/static-analysis
# configuration?" — narrower than "is this Python" (requirements.txt-only
# trees are Python, but have no mypy/pyright config to run). Consumers ask
# this module instead of re-hardcoding marker names.
PYTHON_PACKAGING_MARKERS: tuple[str, ...] = ("pyproject.toml", "setup.py", "setup.cfg")

# At least this many source files must be seen by the extension fallback
# before a language is reported.
MIN_SOURCE_FILES = 1
# Hard bound on how many files a single scan visits.
MAX_SCAN_FILES = 20000


def has_signature_file(workdir: str, sig_file: str) -> bool:
    """True when *sig_file* exists at the root of *workdir* (wildcards allowed)."""
    if any(c in sig_file for c in "*?["):
        return len(glob.glob(os.path.join(workdir, sig_file))) > 0
    return os.path.isfile(os.path.join(workdir, sig_file))


def signature_languages(workdir: str) -> list[str]:
    """All languages with a matching signature file, in table order, deduped.

    The first element is the repo's primary language. Signatures only — no
    extension fallback — because callers like the guard's Go/Rust guards need
    a real ecosystem marker, not an inferred one.
    """
    found: list[str] = []
    for sig_file, language in SIGNATURE_FILES:
        if language in found:
            continue
        if has_signature_file(workdir, sig_file):
            found.append(language)
    return found


def lint_test_commands(language: str | None) -> tuple[str, str] | None:
    """Return ``(lint_command, test_command)`` for *language*, or None."""
    if not language:
        return None
    return LANG_COMMANDS.get(language)


def _skip_dir(name: str) -> bool:
    """True when a directory name is pruned from every scan in this module."""
    if name.startswith("."):
        return True
    if name in SKIP_DIRS:
        return True
    # venv311 / .venv312 / venvs — the guard's scanner prunes these by prefix.
    return name.startswith("venv") or name.startswith(".venv")


def _is_excluded_source(rel_path: str) -> bool:
    """True when a repo-relative path is not a fallback source candidate.

    Only *directory* components are considered: any component in SKIP_DIRS /
    TEST_DIRS, or dot-prefixed, prunes the path. The basename is never
    excluded — see the module docstring.
    """
    parts = rel_path.split("/")
    for part in parts[:-1]:
        if _skip_dir(part) or part in TEST_DIRS:
            return True
    return False


def _git_files(workdir: str) -> list[str] | None:
    """Repo-relative file paths from git, or None when git can't answer.

    Uses ``--cached --others --exclude-standard`` so tracked files and fresh
    untracked (non-ignored) files both count, while gitignored tool junk
    (``.venv``, ``node_modules``, …) does not. GIT_* env vars are stripped —
    the pre-commit hook exports GIT_INDEX_FILE/GIT_DIR and they poison nested
    git calls (same class as DF-008).

    Only answers when *workdir* IS the repository root: run from a
    subdirectory, ``git ls-files`` would list the enclosing repo's files
    (a foreign tree), so anything else falls through to the walk.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if top.returncode != 0:
            return None
        if os.path.realpath(top.stdout.strip()) != os.path.realpath(workdir):
            return None
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=workdir,
            capture_output=True,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace")
    # Drop index entries whose file is gone from the worktree: the tree is
    # what a language answer describes, and a staged-then-deleted source file
    # must not keep a repo looking like that language.
    return [p for p in out.split("\0") if p and os.path.exists(os.path.join(workdir, p))]


def _walk_files(workdir: str) -> list[str]:
    """Bounded os.walk fallback — repo-relative paths, pruned, capped."""
    files: list[str] = []
    for root, dirs, names in os.walk(workdir):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        for name in names:
            files.append(os.path.relpath(os.path.join(root, name), workdir).replace(os.sep, "/"))
            if len(files) >= MAX_SCAN_FILES:
                return files
    return files


def source_files(workdir: str) -> list[str]:
    """Candidate source files for the extension fallback (pruned, capped)."""
    files = _git_files(workdir)
    if files is None or not files:
        files = _walk_files(workdir)
    return [f for f in files if not _is_excluded_source(f)]


def extension_languages(workdir: str) -> list[str]:
    """Languages inferred from source extensions, most files first.

    Ties break on ``SOURCE_EXTENSIONS`` insertion order so the result is
    stable across runs and filesystems.
    """
    order = list(dict.fromkeys(SOURCE_EXTENSIONS.values()))
    counts: dict[str, int] = {}
    for rel_path in source_files(workdir):
        language = SOURCE_EXTENSIONS.get(os.path.splitext(rel_path)[1].lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    ranked = sorted(
        (lang for lang, n in counts.items() if n >= MIN_SOURCE_FILES),
        key=lambda lang: (-counts[lang], order.index(lang)),
    )
    return ranked


def detect_languages(workdir: str) -> list[str]:
    """All detected languages for *workdir* (primary first).

    Signature files win: when any signature matches, only signature-derived
    languages are returned. The extension fallback answers only when no
    signature file matched at all.
    """
    langs = signature_languages(workdir)
    if langs:
        return langs
    return extension_languages(workdir)


def detect_language(workdir: str) -> str | None:
    """The repo's primary language, or None when nothing is detectable."""
    langs = detect_languages(workdir)
    return langs[0] if langs else None


def has_sql_sources(workdir: str) -> bool:
    """True when the tree holds ``*.sql`` files or a ``migrations/`` dir.

    SQL is not a lint/test language here (there is no command pair for it) —
    it selects static-analysis tooling only.
    """
    for root, dirs, names in os.walk(workdir):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        if "migrations" in dirs:
            return True
        if any(n.endswith(".sql") for n in names):
            return True
    return False


def python_packaging_present(workdir: str) -> bool:
    """True when a Python packaging/static-analysis marker exists."""
    return any(has_signature_file(workdir, marker) for marker in PYTHON_PACKAGING_MARKERS)
