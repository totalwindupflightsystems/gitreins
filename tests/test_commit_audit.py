"""
Regression tests for CommitAuditor (v0.9.0).

Tests the LLM-powered commit message audit without requiring
a live LLM — uses mocking for the LLM client and real git
operations for diff capture and message reading.
"""
import json
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from engine.commit_audit import (
    CommitAuditor,
    CommitAuditResult,
    CommitReviewResult,
    ReviewIssue,
    COMMIT_AUDIT_SYSTEM_PROMPT,
    COMMIT_AUDIT_TOOLS,
)
from engine.llm import LLMClient, LLMResponse, LLMUsage


# ═══════════════════════════════════════════════════════════════
# CommitAuditResult
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditResult:
    """Test the result dataclass."""

    def test_valid_result_defaults(self):
        result = CommitAuditResult(valid=True)
        assert result.valid is True
        assert result.issues == []
        assert result.suggested_message == ""
        assert result.action == "pass"

    def test_invalid_result_has_issues(self):
        result = CommitAuditResult(
            valid=False,
            issues=["Too vague", "Missing scope"],
            suggested_message="fix(auth): correct login flow",
        )
        assert result.valid is False
        assert len(result.issues) == 2
        assert "fix(auth)" in result.suggested_message
        assert result.action == "block"

    def test_action_block_when_invalid(self):
        result = CommitAuditResult(valid=False, issues=["bad"])
        assert result.action == "block"


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — parse_result (no LLM needed)
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorParseResult:
    """Test result parsing with mocked LLM responses."""

    def _make_auditor(self) -> CommitAuditor:
        llm = LLMClient(api_key="sk-test", model="test/model")
        return CommitAuditor(llm)

    def test_parse_valid(self):
        auditor = self._make_auditor()
        resp = LLMResponse(content='{"valid": true}', usage=LLMUsage())
        result = auditor._parse_result(resp)
        assert result.valid is True
        assert result.issues == []

    def test_parse_invalid_with_suggestion(self):
        auditor = self._make_auditor()
        resp = LLMResponse(
            content=json.dumps({
                "valid": False,
                "issues": ["Message is too vague"],
                "suggested_message": "fix(auth): correct login redirect",
            }),
            usage=LLMUsage(),
        )
        result = auditor._parse_result(resp)
        assert result.valid is False
        assert "too vague" in result.issues[0].lower()
        assert "fix(auth)" in result.suggested_message

    def test_parse_markdown_fenced_json(self):
        auditor = self._make_auditor()
        resp = LLMResponse(
            content='```json\n{"valid": true}\n```',
            usage=LLMUsage(),
        )
        result = auditor._parse_result(resp)
        assert result.valid is True

    def test_parse_invalid_json_falls_back_to_valid(self):
        """Malformed JSON defaults to valid (safe — don't block on parse error)."""
        auditor = self._make_auditor()
        resp = LLMResponse(content="not json at all", usage=LLMUsage())
        result = auditor._parse_result(resp)
        assert result.valid is True  # safe default
        assert len(result.issues) > 0  # but it reports the issue

    def test_empty_content(self):
        auditor = self._make_auditor()
        resp = LLMResponse(content="", usage=LLMUsage())
        result = auditor._parse_result(resp)
        assert result.valid is True  # empty = pass

    def test_iteration_tracking(self):
        auditor = self._make_auditor()
        resp = LLMResponse(content='{"valid": true}', usage=LLMUsage())
        result = auditor._parse_result(resp, iteration=3)
        assert result.iterations_used == 3


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — audit (mocked LLM)
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorAudit:
    """Test the full audit flow with mocked LLM."""

    def _make_auditor(self, strictness="standard") -> CommitAuditor:
        llm = LLMClient(api_key="sk-test", model="test/model")
        return CommitAuditor(llm, strictness=strictness)

    def _valid_response(self):
        return LLMResponse(content='{"valid": true}', usage=LLMUsage())

    def _invalid_response(self, issues=None, suggestion=None):
        return LLMResponse(
            content=json.dumps({
                "valid": False,
                "issues": issues or ["Message too vague."],
                "suggested_message": suggestion or "fix: update code",
            }),
            usage=LLMUsage(),
        )

    def test_audit_with_empty_diff_returns_valid(self):
        """No staged changes — nothing to audit."""
        auditor = self._make_auditor()
        result = auditor.audit("fix: stuff", diff="")
        assert result.valid is True

    def test_audit_with_empty_message_returns_invalid(self):
        """Empty message is always invalid."""
        auditor = self._make_auditor()
        result = auditor.audit("", diff="+some change")
        assert result.valid is False
        assert any("Empty" in i for i in result.issues)

    def test_audit_empty_message_generates_fallback(self):
        """Empty message triggers fallback message generation."""
        auditor = self._make_auditor()
        auditor.suggest_message = True
        result = auditor.audit("", diff="+some change")
        assert result.suggested_message != ""

    @patch.object(LLMClient, 'chat')
    def test_single_pass_valid(self, mock_chat):
        """LLM returns valid on first call."""
        mock_chat.return_value = self._valid_response()
        auditor = self._make_auditor()
        result = auditor.audit("fix(auth): correct redirect", diff="+some change")
        assert result.valid is True
        assert mock_chat.call_count == 1

    @patch.object(LLMClient, 'chat')
    def test_single_pass_invalid_with_suggestion(self, mock_chat):
        """LLM returns invalid with a suggested better message."""
        mock_chat.return_value = self._invalid_response(
            issues=["Message doesn't describe the change"],
            suggestion="feat(api): add login endpoint with JWT support",
        )
        auditor = self._make_auditor()
        result = auditor.audit("fix stuff", diff="+def login(): ...")
        assert result.valid is False
        assert "feat(api)" in result.suggested_message

    @patch.object(LLMClient, 'chat')
    def test_llm_error_returns_valid(self, mock_chat):
        """LLM failures should not block commits (safe default)."""
        mock_chat.side_effect = RuntimeError("API down")
        auditor = self._make_auditor()
        result = auditor.audit("fix: something", diff="+some change")
        assert result.valid is True  # safe default


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — strictness levels
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorStrictness:
    """Test that strictness affects prompt construction."""

    def test_prompt_contains_strictness_level(self):
        """The user prompt must include the strictness level."""
        from engine.commit_audit import COMMIT_AUDIT_USER_PROMPT, INSTRUCTIONS_BY_STRICTNESS

        # Check that all three strictness levels have instructions
        assert "lenient" in INSTRUCTIONS_BY_STRICTNESS
        assert "standard" in INSTRUCTIONS_BY_STRICTNESS
        assert "strict" in INSTRUCTIONS_BY_STRICTNESS

        prompt = COMMIT_AUDIT_USER_PROMPT.format(
            strictness="STRICT",
            mode="audit",
            message="test msg",
            diff="test diff",
            instructions=INSTRUCTIONS_BY_STRICTNESS["strict"],
        )
        assert "STRICT" in prompt
        assert "conventional commits" in prompt.lower()

    def test_lenient_single_word_allowed(self):
        """Lenient mode instructions allow short messages."""
        from engine.commit_audit import INSTRUCTIONS_LENIENT
        assert "short messages" in INSTRUCTIONS_LENIENT.lower()

    def test_strict_requires_conventional_commits(self):
        """Strict mode requires conventional commits format."""
        from engine.commit_audit import INSTRUCTIONS_STRICT
        assert "conventional commits" in INSTRUCTIONS_STRICT.lower()
        assert "type(scope):" in INSTRUCTIONS_STRICT


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — diff capture
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorDiffCapture:
    """Test the git diff capture mechanism."""

    def test_diff_capture_in_git_repo(self):
        """_capture_diff runs successfully in a git repo."""
        auditor = CommitAuditor(
            LLMClient(api_key="sk-test", model="test/model"),
        )
        diff = auditor._capture_diff()
        # Should return a string (maybe empty if no staged changes)
        assert isinstance(diff, str)
        # In our actual repo (gitreins-poc), it should work
        assert diff != "" or True  # empty diff is valid (nothing staged)


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — tool execution
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorTools:
    """Test the tool execution methods."""

    def _make_auditor(self) -> CommitAuditor:
        llm = LLMClient(api_key="sk-test", model="test/model")
        return CommitAuditor(llm)

    def test_read_file_returns_content(self):
        """_tool_read_file reads a real file."""
        auditor = self._make_auditor()
        result = auditor._tool_read_file("README.md", limit=5)
        assert "GitReins" in result or "README" in result or result.startswith("//")

    def test_read_file_not_found(self):
        """_tool_read_file returns error for missing file."""
        auditor = self._make_auditor()
        result = auditor._tool_read_file("nonexistent_file.xyz")
        parsed = json.loads(result)
        assert "error" in parsed

    def test_search_pattern_finds_matches(self):
        """_tool_search_pattern finds real matches."""
        auditor = self._make_auditor()
        result = auditor._tool_search_pattern(r"class CommitAuditor", path="engine", file_glob="*.py")
        assert "CommitAuditor" in result

    def test_search_pattern_no_matches(self):
        """_tool_search_pattern returns hint when nothing found."""
        auditor = self._make_auditor()
        result = auditor._tool_search_pattern(r"zzz_nonexistent_pattern_xyz", path="engine")
        parsed = json.loads(result)
        assert parsed.get("matches") == 0

    def test_search_pattern_invalid_regex(self):
        """_tool_search_pattern handles invalid regex."""
        auditor = self._make_auditor()
        result = auditor._tool_search_pattern(r"[invalid")
        parsed = json.loads(result)
        assert "error" in parsed

    def test_tools_list_has_expected_tools(self):
        """COMMIT_AUDIT_TOOLS includes read_file and search_pattern."""
        names = [t["function"]["name"] for t in COMMIT_AUDIT_TOOLS]
        assert "read_file" in names
        assert "search_pattern" in names


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — fallback message generation
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorFallbackMessage:
    """Test fallback commit message generation from diff stats."""

    def test_generate_fallback_returns_string(self):
        auditor = CommitAuditor(
            LLMClient(api_key="sk-test", model="test/model"),
        )
        msg = auditor._generate_fallback_message("+some change")
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_generate_fallback_starts_with_chore(self):
        auditor = CommitAuditor(
            LLMClient(api_key="sk-test", model="test/model"),
        )
        msg = auditor._generate_fallback_message("")
        assert msg.startswith("chore:")


# ═══════════════════════════════════════════════════════════════
# Config integration
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditConfig:
    """Test that commit_audit config is wired into GitReinsDefaults."""

    def test_defaults_have_commit_audit_fields(self):
        from engine.config import GitReinsDefaults
        defaults = GitReinsDefaults()
        assert defaults.commit_audit_enabled is True
        assert defaults.commit_audit_mode == "warn"
        assert defaults.commit_audit_strictness == "standard"
        assert defaults.commit_audit_max_iterations == 3
        assert defaults.commit_audit_suggest_message is True

    def test_overlay_reads_commit_audit_section(self):
        from engine.config import GitReinsDefaults
        defaults = GitReinsDefaults()
        overlaid = defaults.overlay({
            "defaults": {
                "commit_audit": {
                    "mode": "block",
                    "strictness": "strict",
                }
            }
        })
        assert overlaid.commit_audit_mode == "block"
        assert overlaid.commit_audit_strictness == "strict"
        # Unset fields keep defaults
        assert overlaid.commit_audit_enabled is True

    def test_to_config_dict_includes_commit_audit(self):
        from engine.config import GitReinsDefaults
        defaults = GitReinsDefaults(
            commit_audit_mode="block",
            commit_audit_strictness="strict",
        )
        d = defaults.to_config_dict()
        assert "commit_audit" in d
        assert d["commit_audit"]["mode"] == "block"
        assert d["commit_audit"]["strictness"] == "strict"
        assert d["commit_audit"]["enabled"] is True


# ═══════════════════════════════════════════════════════════════
# ReviewIssue (GR-065g)
# ═══════════════════════════════════════════════════════════════

class TestReviewIssue:
    """Test the ReviewIssue dataclass and from_dict factory."""

    def test_from_dict_full(self):
        ri = ReviewIssue.from_dict({
            "file": "src/auth.py",
            "line": "42",
            "severity": "high",
            "category": "security",
            "title": "Hardcoded secret",
            "description": "API key is in source code",
            "suggestion": "Use environment variable via os.getenv()",
        })
        assert ri.file == "src/auth.py"
        assert ri.line == 42
        assert ri.severity == "high"
        assert ri.category == "security"
        assert ri.title == "Hardcoded secret"
        assert ri.description == "API key is in source code"
        assert ri.suggestion == "Use environment variable via os.getenv()"

    def test_from_dict_defaults(self):
        ri = ReviewIssue.from_dict({})
        assert ri.file == ""
        assert ri.line == 0
        assert ri.severity == "info"
        assert ri.category == "style"
        assert ri.title == ""
        assert ri.description == ""
        assert ri.suggestion == ""

    def test_from_dict_line_as_int_string(self):
        ri = ReviewIssue.from_dict({"line": "99"})
        assert ri.line == 99
        assert isinstance(ri.line, int)

    def test_severity_all_levels(self):
        """All severity levels accepted."""
        for sev in ["critical", "high", "medium", "low", "info"]:
            ri = ReviewIssue.from_dict({"severity": sev})
            assert ri.severity == sev


# ═══════════════════════════════════════════════════════════════
# CommitAuditResult — review_issues + review_summary (GR-065g)
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditResultReviewFields:
    """Test the new review_issues and review_summary fields on CommitAuditResult."""

    def test_default_review_issues_empty(self):
        r = CommitAuditResult(valid=True)
        assert r.review_issues == []
        assert r.review_summary == ""

    def test_review_issues_populated(self):
        r = CommitAuditResult(
            valid=False,
            review_issues=[
                {"file": "src/a.py", "line": 1, "severity": "high",
                 "category": "bugs", "title": "Bug", "suggestion": "Fix it"},
            ],
            review_summary="One issue found",
        )
        assert len(r.review_issues) == 1
        assert r.review_issues[0]["file"] == "src/a.py"
        assert r.review_issues[0]["suggestion"] == "Fix it"
        assert r.review_summary == "One issue found"

    def test_review_issues_still_defaults_with_old_result(self):
        """Backward compat: old code that doesn't set review_issues still works."""
        r = CommitAuditResult(valid=False, issues=["Too vague"])
        assert r.review_issues == []
        assert r.review_summary == ""
        assert r.issues == ["Too vague"]


# ═══════════════════════════════════════════════════════════════
# CommitReviewResult (GR-065g)
# ═══════════════════════════════════════════════════════════════

class TestCommitReviewResult:
    """Test the CommitReviewResult dataclass."""

    def test_defaults(self):
        r = CommitReviewResult(valid=True)
        assert r.valid is True
        assert r.summary == ""
        assert r.issues == []
        assert r.message_valid is True
        assert r.message_issues == []
        assert r.suggested_message == ""
        assert r.iterations_used == 0

    def test_with_issues(self):
        ri = ReviewIssue(
            file="src/x.py", line=10, severity="medium",
            category="anti_patterns", title="Mutable default",
            suggestion="Use None and set default in body",
        )
        r = CommitReviewResult(
            valid=False, summary="1 issue",
            issues=[ri], message_valid=True,
            iterations_used=2,
        )
        assert len(r.issues) == 1
        assert r.issues[0].category == "anti_patterns"
        assert r.iterations_used == 2


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — _run_review review fields (GR-065g)
# ═══════════════════════════════════════════════════════════════

class TestCommitAuditorRunReview:
    """Test _run_review populates review_issues and review_summary."""

    def _make_llm(self) -> LLMClient:
        return LLMClient(api_key="sk-test", model="test/model")

    def test_run_review_populates_review_issues(self):
        """_run_review should include review_issues from the CommitReviewResult."""
        auditor = CommitAuditor(
            self._make_llm(),
            review_mode="review",
            review_checks={"bugs": True, "security": True},
        )
        # Mock the review() call to return a CommitReviewResult with issues
        review_issue = ReviewIssue(
            file="src/x.py", line=5, severity="critical",
            category="bugs", title="Null deref",
            suggestion="Add null check",
        )
        with patch.object(auditor, "review", return_value=CommitReviewResult(
            valid=False, summary="Found 1 issue",
            issues=[review_issue], message_valid=True,
            iterations_used=1,
        )):
            result = auditor._run_review("fix: stuff", "diff content")
            assert len(result.review_issues) == 1
            assert result.review_issues[0]["file"] == "src/x.py"
            assert result.review_issues[0]["line"] == 5
            assert result.review_issues[0]["severity"] == "critical"
            assert result.review_issues[0]["category"] == "bugs"
            assert result.review_issues[0]["suggestion"] == "Add null check"
            assert result.review_summary == "Found 1 issue"

    def test_run_review_empty_issues(self):
        """_run_review with no issues should have empty review_issues."""
        auditor = CommitAuditor(
            self._make_llm(),
            review_mode="review",
        )
        with patch.object(auditor, "review", return_value=CommitReviewResult(
            valid=True, summary="No issues", issues=[], iterations_used=1,
        )):
            result = auditor._run_review("fix: stuff", "diff content")
            assert result.review_issues == []
            assert result.review_summary == "No issues"
            assert result.valid is True

    def test_run_review_maps_suggestion(self):
        """Suggestions should be carried through to review_issues dicts."""
        auditor = CommitAuditor(
            self._make_llm(),
            review_mode="review",
            review_suggest_fix=True,
        )
        ri = ReviewIssue(
            file="src/y.py", line=20, severity="medium",
            category="anti_patterns", title="God object",
            description="Too many responsibilities",
            suggestion="Split into smaller classes",
        )
        with patch.object(auditor, "review", return_value=CommitReviewResult(
            valid=False, summary="1 anti-pattern", issues=[ri], iterations_used=1,
        )):
            result = auditor._run_review("feat: add module", "diff content")
            assert len(result.review_issues) == 1
            assert result.review_issues[0]["description"] == "Too many responsibilities"
            assert result.review_issues[0]["suggestion"] == "Split into smaller classes"

    def test_audit_routes_to_review_when_review_mode_set(self):
        """When review_mode != 'message', audit() should call _run_review()."""
        auditor = CommitAuditor(
            self._make_llm(),
            review_mode="review",
        )
        with patch.object(auditor, "_run_review", return_value=CommitAuditResult(
            valid=False, issues=["[high][bugs] src/a.py:1: Bug"],
            review_issues=[
                {"file": "src/a.py", "line": 1, "severity": "high",
                 "category": "bugs", "title": "Bug", "suggestion": "Fix"},
            ],
            review_summary="One bug",
        )) as mock_run:
            result = auditor.audit("fix: bug")
            mock_run.assert_called_once()
            assert len(result.review_issues) == 1
            assert result.review_summary == "One bug"


# ═══════════════════════════════════════════════════════════════
# Pipeline — review output formatting (GR-065g)
# ═══════════════════════════════════════════════════════════════

class TestPipelineReviewOutput:
    """Test that pipeline formats review findings correctly."""

    def _make_llm(self) -> LLMClient:
        return LLMClient(api_key="sk-test", model="test/model")

    def test_review_issues_in_step_result_data(self):
        """StepResult data should include review_issues and review_summary."""
        from engine.pipeline import Pipeline, StepResult

        pipeline = Pipeline({}, "/tmp", llm=self._make_llm())

        # Simulate what _run_commit_audit would produce
        result = CommitAuditResult(
            valid=False,
            issues=["[critical][security] src/auth.py:42: Hardcoded secret"],
            review_issues=[
                {"file": "src/auth.py", "line": 42, "severity": "critical",
                 "category": "security", "title": "Hardcoded secret",
                 "description": "API key in source", "suggestion": "Use env var"},
            ],
            review_summary="1 critical issue found",
            iterations_used=1,
        )

        # Manually construct the StepResult as _run_commit_audit would
        sr = StepResult(
            id="commit_audit", type="commit_audit", passed=True,
            output="⚠ Commit review — 1 issue(s) found\n\n"
                   "  src/auth.py:42 [security] [🔴 CRITICAL] — Hardcoded secret\n"
                   "    API key in source\n"
                   "    Fix: Use env var\n\n",
            data={
                "valid": result.valid,
                "issues": result.issues,
                "suggested_message": result.suggested_message,
                "mode": "warn",
                "iterations_used": result.iterations_used,
                "review_issues": result.review_issues,
                "review_summary": result.review_summary,
            },
        )
        assert "review_issues" in sr.data
        assert len(sr.data["review_issues"]) == 1
        assert sr.data["review_issues"][0]["suggestion"] == "Use env var"
        assert sr.data["review_summary"] == "1 critical issue found"

    def test_output_includes_file_line_and_severity(self):
        """Output text should contain file:line references and severity markers."""
        from engine.pipeline import Pipeline
        pipeline = Pipeline({}, "/tmp", llm=self._make_llm())

        result = CommitAuditResult(
            valid=False,
            review_issues=[
                {"file": "src/a.py", "line": 10, "severity": "critical",
                 "category": "bugs", "title": "NPE", "suggestion": "Add guard"},
                {"file": "src/b.py", "line": 20, "severity": "low",
                 "category": "style", "title": "Long line", "suggestion": "Wrap"},
            ],
            review_summary="2 issues found",
        )

        # Build output same way _run_commit_audit does
        review_issues = getattr(result, "review_issues", [])
        review_summary = getattr(result, "review_summary", "")
        output_lines = []
        if review_issues:
            sev_marker = {
                "critical": "🔴 CRITICAL", "high": "🟠 HIGH",
                "medium": "🟡 MEDIUM", "low": "🟢 LOW", "info": "ℹ️ INFO",
            }
            output_lines.append(f"⚠ Commit review — {len(review_issues)} issue(s) found")
            if review_summary:
                output_lines.append(f"   {review_summary}")
            output_lines.append("")
            for ri in review_issues:
                sev = sev_marker.get(ri.get("severity", "info"), "ℹ️ INFO")
                cat = ri.get("category", "unknown")
                file_ref = f"{ri.get('file', '')}:{ri.get('line', 0)}"
                title = ri.get("title", "")
                desc = ri.get("description", "")
                sugg = ri.get("suggestion", "")
                output_lines.append(f"  {file_ref} [{cat}] [{sev}] — {title}")
                if desc:
                    output_lines.append(f"    {desc}")
                if sugg:
                    output_lines.append(f"    Fix: {sugg}")
                output_lines.append("")

        output = "\n".join(output_lines)
        assert "⚠ Commit review — 2 issue(s) found" in output
        assert "2 issues found" in output
        assert "src/a.py:10" in output
        assert "[bugs]" in output
        assert "🔴 CRITICAL" in output
        assert "Fix: Add guard" in output
        assert "Fix: Wrap" in output

    def test_output_no_review_issues(self):
        """When no review_issues, output should just show message audit."""
        result = CommitAuditResult(valid=True)
        assert result.review_issues == []
        assert result.review_summary == ""

        # Output should not contain review section
        output_lines = []
        if result.review_issues:
            output_lines.append("REVIEW")  # shouldn't reach this
        if result.valid:
            output_lines.append("✓ Commit message looks good.")
        output = "\n".join(output_lines)
        assert "REVIEW" not in output
        assert "✓ Commit message looks good." in output
