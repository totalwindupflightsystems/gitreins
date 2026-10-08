"""Tests for `gitreins setup-tools` (DF-GITREINS-POC-69).

The command used to print a multi-language header over a single-language
tool list: the header named every detected language (``lang['name']`` —
"Python + C + SQL" on this repo) while the tool list was keyed on the
PRIMARY type only (``lang_tools_map`` on ``lang['type']`` → mypy/pyright),
so the header promised C/SQL coverage the list never delivered. The
missing-tool guidance was a bare ``pip install <tool>`` line — exactly what
PEP 668 (externally-managed environment) blocks on a stock Debian install
(filed three times as POC-64 for the product itself).

Contract tested here:
1. The header names only languages whose tools are actually listed; a
   detected language with no tracked tools is disclosed separately, never
   folded into the header.
2. Install guidance is pipx/uv-tool based for Python tools with the
   tool-specific route named per tool — no bare ``pip install`` anywhere in
   _TOOL_INSTALL_GUIDE output.
3. The unknown-language zero-state (exit 0, "No static analysis tools are
   tracked for <lang>.") is preserved.
"""

import os
import shutil

from tests.test_cli import _init_real_git_repo, run_cli


def _git_only_env(tmp_path):
    """PATH with git and nothing else: every static-analysis tool is missing.

    Mirrors test_cli's _tool_path helper — the host PATH resolves real tools
    (mypy on this box), which would make the missing-tool guidance
    unreachable and the counts non-deterministic.
    """
    bin_dir = tmp_path / "bin-git-only"
    bin_dir.mkdir()
    git = shutil.which("git")
    if git:
        os.symlink(git, bin_dir / "git")
    return {"PATH": str(bin_dir)}


def _write(repo, rel, text) -> None:
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _mixed_repo(tmp_path):
    """A Python + C + SQL repo — the DF-GITREINS-POC-69 repro shape.

    python and c come from signature files (pyproject.toml; Makefile maps to
    "c" per SIGNATURE_FILES), sql from the migrations/ directory via
    has_sql_sources. The extension fallback never fires (a signature file
    exists), so no source files are needed — exactly this repo's shape.
    """
    repo = _init_real_git_repo(tmp_path)
    _write(repo, "pyproject.toml", "[project]\nname = 'mixed'\n")
    _write(repo, "Makefile", "lint:\n\techo lint\ntest:\n\techo test\n")
    _write(repo, os.path.join("migrations", "001_init.sql"), "CREATE TABLE t (id INTEGER);\n")
    return repo


class TestSetupToolsHeaderMatchesList:
    """Criterion 1: the header and the tool list never contradict each other."""

    def test_mixed_repo_header_lists_only_languages_whose_tools_are_listed(self, tmp_path) -> None:
        repo = _mixed_repo(tmp_path)
        result = run_cli("setup-tools", cwd=repo, extra_env=_git_only_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        # All three detected languages are tracked, so the header names all
        # three AND the list covers each of them: python → mypy/pyright,
        # c → cppcheck (Makefile signature), sql → sqlfluff (migrations/).
        assert "Static Analysis Tools for Python + C + SQL:" in result.stdout
        assert "mypy" in result.stdout
        assert "pyright" in result.stdout
        assert "sqlfluff" in result.stdout
        assert "cppcheck" in result.stdout
        # No untracked-language disclosure when every detected language is covered.
        assert "No tracked static analysis tools" not in result.stdout

    def test_tools_are_listed_in_detection_order(self, tmp_path) -> None:
        repo = _mixed_repo(tmp_path)
        result = run_cli("setup-tools", cwd=repo, extra_env=_git_only_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        # python is the primary language (signature-table order): its tools
        # come first, sql's tool last.
        assert result.stdout.index("pyright") < result.stdout.index("sqlfluff")

    def test_pure_python_repo_output_is_unchanged(self, tmp_path) -> None:
        repo = _init_real_git_repo(tmp_path)
        _write(repo, "pyproject.toml", "[project]\nname = 'pyonly'\n")
        result = run_cli("setup-tools", cwd=repo, extra_env=_git_only_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Static Analysis Tools for Python:" in result.stdout
        assert "mypy" in result.stdout
        assert "pyright" in result.stdout
        assert "sqlfluff" not in result.stdout
        # No untracked-language disclosure when nothing else was detected.
        assert "No tracked static analysis tools" not in result.stdout

    def test_sql_only_repo_lists_sqlfluff(self, tmp_path) -> None:
        # Latent bug fixed alongside: the old primary-type keying left an
        # SQL-only repo in the zero state even though sqlfluff is tracked.
        repo = _init_real_git_repo(tmp_path)
        _write(repo, os.path.join("migrations", "001_init.sql"), "CREATE TABLE t (id INTEGER);\n")
        result = run_cli("setup-tools", cwd=repo, extra_env=_git_only_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Static Analysis Tools for SQL:" in result.stdout
        assert "sqlfluff" in result.stdout


class TestSetupToolsInstallGuidance:
    """Criterion 2: pipx/uv-tool guidance per tool, never a bare pip line."""

    def test_missing_tool_lines_name_pep668_safe_routes(self, tmp_path) -> None:
        repo = _mixed_repo(tmp_path)
        result = run_cli("setup-tools", cwd=repo, extra_env=_git_only_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        # No bare `pip install <tool>` — PEP 668 blocks it on stock Linux.
        assert "pip install" not in result.stdout
        lines = result.stdout.splitlines()

        def tool_line(prefix):
            matches = [line for line in lines if line.strip().startswith(prefix)]
            assert matches, f"no output line for {prefix}"
            return matches[0]

        mypy_line = tool_line("mypy")
        pyright_line = tool_line("pyright")
        sqlfluff_line = tool_line("sqlfluff")
        # Tool-specific routes are named per tool (POC-64/POC-69).
        assert "pipx install mypy" in mypy_line
        assert "uv tool install mypy" in mypy_line
        assert "npm install -g pyright" in pyright_line
        assert "pipx install pyright" in pyright_line
        assert "pipx install sqlfluff" in sqlfluff_line
        assert "uv tool install sqlfluff" in sqlfluff_line

    def test_setup_tools_guide_has_no_bare_pip_lines(self) -> None:
        from gitreins.cli import _SETUP_TOOLS_INSTALL_GUIDE

        assert _SETUP_TOOLS_INSTALL_GUIDE, "the install guide must not be empty"
        for tool, guide in _SETUP_TOOLS_INSTALL_GUIDE.items():
            assert "pip install" not in guide, (
                f"{tool}: bare 'pip install' violates PEP 668 guidance: {guide}"
            )

    def test_setup_tools_guide_covers_every_tracked_tool(self) -> None:
        from gitreins.cli import _SETUP_TOOLS_INSTALL_GUIDE, _SETUP_TOOLS_LANG_TOOLS

        tracked = {tool for tools in _SETUP_TOOLS_LANG_TOOLS.values() for tool in tools}
        tracked.add("sqlfluff")  # sql is keyed via has_sql_sources, not the dict
        missing = tracked - set(_SETUP_TOOLS_INSTALL_GUIDE)
        assert not missing, f"tracked tools without an install route: {sorted(missing)}"

    def test_setup_tools_tools_are_tracked_in_the_engine_registry(self) -> None:
        """The tools setup-tools offers must be findable (find_tool knows them)."""
        from engine.static_analysis import _TOOL_BINARIES
        from gitreins.cli import _SETUP_TOOLS_LANG_TOOLS

        tracked = {tool for tools in _SETUP_TOOLS_LANG_TOOLS.values() for tool in tools}
        unregistered = tracked - set(_TOOL_BINARIES)
        assert not unregistered, (
            f"setup-tools offers tools the engine registry cannot find: {sorted(unregistered)}"
        )

    def test_setup_tools_guide_names_a_route_per_tracked_tool(self) -> None:
        """POC-64: tool-specific routes — pyright via npm/pipx, mypy via pipx/uv."""
        from gitreins.cli import _SETUP_TOOLS_INSTALL_GUIDE

        assert "pipx install mypy" in _SETUP_TOOLS_INSTALL_GUIDE["mypy"]
        assert "uv tool install mypy" in _SETUP_TOOLS_INSTALL_GUIDE["mypy"]
        assert "npm install -g pyright" in _SETUP_TOOLS_INSTALL_GUIDE["pyright"]
        assert "pipx install pyright" in _SETUP_TOOLS_INSTALL_GUIDE["pyright"]
        assert "pipx install sqlfluff" in _SETUP_TOOLS_INSTALL_GUIDE["sqlfluff"]
        assert "uv tool install sqlfluff" in _SETUP_TOOLS_INSTALL_GUIDE["sqlfluff"]


class TestSetupToolsZeroState:
    """Criterion 3: the unknown-language zero state is preserved."""

    def test_unknown_language_zero_state_exit_0(self, tmp_path) -> None:
        # _init_real_git_repo's tree holds only base.txt: no signature file,
        # no source extension, no SQL sources → nothing tracked for "unknown".
        repo = _init_real_git_repo(tmp_path)
        result = run_cli("setup-tools", cwd=repo, extra_env=_git_only_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "No static analysis tools are tracked for unknown." in result.stdout
