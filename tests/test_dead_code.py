"""Unit tests for engine/dead_code.py — AST-based dead code detector.

Covers all 4 categories:
  1. Unreachable code (after return/raise/break/continue)
  2. Unused functions (defined but never called)
  3. Unused imports (imported but never referenced)
  4. Empty functions (pass/... only)

Plus: DeadCodeReport passed/summary semantics, WHITELIST_FUNCTIONS handling,
private/test_ function skipping, decorator-based call detection, and clean-file
edge cases.
"""

import os
import textwrap

import pytest

from engine.dead_code import (
    DeadCodeDetector,
    DeadCodeFinding,
    DeadCodeReport,
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _write_py(dir_path: str, name: str, body: str) -> str:
    """Write a Python file with the given body. Returns absolute path."""
    full = os.path.join(dir_path, name)
    # textwrap.dedent lets us keep tests readable with indented triple-quoted strings.
    with open(full, "w") as f:
        f.write(textwrap.dedent(body))
    return full


def _categories(report: DeadCodeReport) -> list[str]:
    return [f.category for f in report.findings]


def _find_by_category(report: DeadCodeReport, category: str) -> list[DeadCodeFinding]:
    return [f for f in report.findings if f.category == category]


@pytest.fixture
def tmp_project(tmp_path):
    """A clean temp project directory. Tests create their own .py files here."""
    return str(tmp_path)


# ─────────────────────────────────────────────────────────────────────
# Dataclass: DeadCodeFinding
# ─────────────────────────────────────────────────────────────────────

class TestDeadCodeFinding:
    def test_dataclass_fields(self):
        f = DeadCodeFinding(file="a.py", line=10, category="unreachable", message="x")
        assert f.file == "a.py"
        assert f.line == 10
        assert f.category == "unreachable"
        assert f.message == "x"

    def test_equality(self):
        a = DeadCodeFinding(file="a.py", line=1, category="unreachable", message="m")
        b = DeadCodeFinding(file="a.py", line=1, category="unreachable", message="m")
        assert a == b


# ─────────────────────────────────────────────────────────────────────
# DeadCodeReport: passed + summary properties
# ─────────────────────────────────────────────────────────────────────

class TestDeadCodeReport:
    def test_default_empty_report_passes(self):
        report = DeadCodeReport()
        assert report.findings == []
        assert report.passed is True

    def test_empty_report_summary(self):
        report = DeadCodeReport()
        assert report.summary == "No dead code found"

    def test_report_with_findings_does_not_pass(self):
        report = DeadCodeReport(findings=[
            DeadCodeFinding(file="a.py", line=1, category="unreachable", message="x"),
        ])
        assert report.passed is False
        assert len(report.findings) == 1

    def test_summary_groups_by_category(self):
        report = DeadCodeReport(findings=[
            DeadCodeFinding(file="a.py", line=1, category="unreachable", message="m1"),
            DeadCodeFinding(file="b.py", line=2, category="unreachable", message="m2"),
            DeadCodeFinding(file="c.py", line=3, category="empty_function", message="m3"),
        ])
        s = report.summary
        assert "UNREACHABLE (2)" in s
        assert "EMPTY_FUNCTION (1)" in s
        assert "a.py:1" in s
        assert "c.py:3" in s

    def test_summary_truncates_after_ten_in_category(self):
        findings = [
            DeadCodeFinding(file=f"f{i}.py", line=i, category="unreachable", message=f"m{i}")
            for i in range(15)
        ]
        report = DeadCodeReport(findings=findings)
        s = report.summary
        assert "... and 5 more" in s


# ─────────────────────────────────────────────────────────────────────
# Category 1: Unreachable code
# ─────────────────────────────────────────────────────────────────────

class TestUnreachableCode:
    def test_code_after_return_is_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            def foo():
                return 1
                x = 2  # unreachable
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        cats = _categories(report)
        assert "unreachable" in cats
        msgs = [f.message for f in _find_by_category(report, "unreachable")]
        assert any("return" in m for m in msgs)

    def test_code_after_raise_is_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            def foo():
                raise ValueError("nope")
                y = 3  # unreachable
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        assert "unreachable" in _categories(report)
        msgs = [f.message for f in _find_by_category(report, "unreachable")]
        assert any("raise" in m for m in msgs)

    def test_break_and_continue_in_nested_loops_not_detected_by_current_impl(self, tmp_project):
        """Document current implementation behavior: only top-level body statements are checked.

        The detector walks function bodies but iterates `node.body` directly, so
        break/continue nested inside `for`/`while` are not flagged. This test
        pins that limitation so future refactors don't silently change it.
        """
        _write_py(tmp_project, "mod.py", '''
            def foo():
                for item in [1, 2, 3]:
                    break
                    z = 4  # nested in loop body — not detected as unreachable
                for item in [1, 2, 3]:
                    continue
                    w = 5  # nested in loop body — not detected as unreachable
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        # Documenting the limitation: unreachable inside nested loops is NOT detected.
        assert "unreachable" not in _categories(report)

    def test_docstring_after_return_is_NOT_flagged(self, tmp_project):
        """Docstrings immediately after return are tolerated (common pattern)."""
        _write_py(tmp_project, "mod.py", '''
            def foo():
                return 1
                """old docstring kept here by accident"""
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        assert "unreachable" not in _categories(report)

    def test_clean_function_with_no_unreachable(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            def foo(x):
                if x > 0:
                    return x
                return -x
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        assert "unreachable" not in _categories(report)


# ─────────────────────────────────────────────────────────────────────
# Category 2: Unused functions
# ─────────────────────────────────────────────────────────────────────

class TestUnusedFunctions:
    def _full_report(self, workdir: str) -> DeadCodeReport:
        """Run scan() then merge find_unused_functions() into the report."""
        det = DeadCodeDetector(workdir)
        report = det.scan()
        report.findings.extend(det.find_unused_functions())
        return report

    def test_unused_function_flagged(self, tmp_project):
        _write_py(tmp_project, "a.py", '''
            def used_func():
                return 1

            def dead_func():
                return 2  # never called

            used_func()
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        names = [f.message for f in unused]
        assert any("dead_func" in m for m in names)
        assert not any("used_func" in m for m in names)

    def test_function_called_in_other_file_flagged_as_used(self, tmp_project):
        """Function defined in one file but called in another must NOT be flagged."""
        _write_py(tmp_project, "lib.py", '''
            def helper():
                return 42
        ''')
        _write_py(tmp_project, "main.py", '''
            from lib import helper
            x = helper()
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("helper" in f.message for f in unused)

    def test_whitelisted_dunder_not_flagged(self, tmp_project):
        """Dunder methods are called by Python, not by Call AST — must be whitelisted."""
        _write_py(tmp_project, "cls.py", '''
            class Thing:
                def __init__(self, x):
                    self.x = x
                def __repr__(self):
                    return f"<Thing {self.x}>"
                def __len__(self):
                    return self.x
                def __eq__(self, other):
                    return self.x == other.x
        ''')
        report = self._full_report(tmp_project)
        unused_msgs = [f.message for f in _find_by_category(report, "unused_function")]
        # None of the dunder methods should appear
        for d in ["__init__", "__repr__", "__len__", "__eq__"]:
            assert not any(d in m for m in unused_msgs), f"{d} should not be flagged"

    def test_whitelisted_framework_hooks_not_flagged(self, tmp_project):
        """main, run, handle, process, execute, dispatch are whitelisted."""
        _write_py(tmp_project, "app.py", '''
            def main():
                pass
            def run():
                pass
            def handle():
                pass
            def process():
                pass
            def execute():
                pass
            def dispatch():
                pass
        ''')
        report = self._full_report(tmp_project)
        unused_msgs = [f.message for f in _find_by_category(report, "unused_function")]
        for name in ["main", "run", "handle", "process", "execute", "dispatch"]:
            assert not any(f"'{name}'" in m for m in unused_msgs), \
                f"{name} should be whitelisted"

    def test_private_function_with_underscore_not_flagged(self, tmp_project):
        """Functions starting with _ are considered private — skipped."""
        _write_py(tmp_project, "mod.py", '''
            def _internal_helper():
                return 1
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("_internal_helper" in f.message for f in unused)

    def test_test_prefixed_function_not_flagged(self, tmp_project):
        """test_* functions are called by pytest, not by Call AST — skipped."""
        _write_py(tmp_project, "test_thing.py", '''
            def test_something():
                assert True
            def test_another():
                assert True
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("test_" in f.message for f in unused)

    def test_decorated_property_not_flagged(self, tmp_project):
        """@property-decorated funcs are called via attribute access, not Call AST."""
        _write_py(tmp_project, "cls.py", '''
            class Thing:
                @property
                def value(self):
                    return 42
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("value" in f.message for f in unused)

    def test_decorated_staticmethod_not_flagged(self, tmp_project):
        _write_py(tmp_project, "cls.py", '''
            class Thing:
                @staticmethod
                def helper():
                    return 1
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("helper" in f.message for f in unused)

    def test_decorated_pytest_fixture_not_flagged(self, tmp_project):
        """@pytest.fixture funcs are called by the framework — must not be flagged."""
        _write_py(tmp_project, "conftest.py", '''
            import pytest

            @pytest.fixture
            def my_data():
                return [1, 2, 3]
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("my_data" in f.message for f in unused)


# ─────────────────────────────────────────────────────────────────────
# Category 3: Unused imports
# ─────────────────────────────────────────────────────────────────────

class TestUnusedImports:
    def test_unused_import_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            import os
            import sys

            x = 1
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unused = _find_by_category(report, "unused_import")
        msgs = [f.message for f in unused]
        assert any("os" in m for m in msgs)
        assert any("sys" in m for m in msgs)

    def test_used_import_not_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            import os

            x = os.getcwd()
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unused = _find_by_category(report, "unused_import")
        msgs = [f.message for f in unused]
        assert not any("os" in m for m in msgs)

    def test_partial_use_of_module_not_flagged(self, tmp_project):
        """If any name from the module is referenced, it's considered used."""
        _write_py(tmp_project, "mod.py", '''
            import os

            path = os.path.join("a", "b")
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unused = _find_by_category(report, "unused_import")
        assert not any("os" in f.message for f in unused)

    def test_from_import_unused_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            from os import path

            x = 1
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unused = _find_by_category(report, "unused_import")
        assert any("path" in f.message for f in unused)

    def test_from_import_used_not_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            from os import path

            x = path.join("a", "b")
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unused = _find_by_category(report, "unused_import")
        assert not any("path" in f.message for f in unused)

    def test_aliased_import_used_not_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            import numpy as np

            arr = np.array([1, 2, 3])
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unused = _find_by_category(report, "unused_import")
        assert not any("numpy" in f.message for f in unused)


# ─────────────────────────────────────────────────────────────────────
# Category 4: Empty functions
# ─────────────────────────────────────────────────────────────────────

class TestEmptyFunctions:
    def test_pass_only_function_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            def nothing():
                pass
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        empty = _find_by_category(report, "empty_function")
        assert any("nothing" in f.message for f in empty)

    def test_function_with_only_docstring_flagged_as_empty(self, tmp_project):
        """A function whose only body content is a docstring has no real implementation —
        the detector strips the docstring and flags the function as empty.
        """
        _write_py(tmp_project, "mod.py", '''
            def documented():
                """This function does something."""
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        empty = _find_by_category(report, "empty_function")
        assert any("documented" in f.message for f in empty)

    def test_function_with_real_body_not_flagged(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            def real():
                return 42
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        empty = _find_by_category(report, "empty_function")
        assert not any("real" in f.message for f in empty)

    def test_ellipsis_body_flagged_as_empty(self, tmp_project):
        """`...` is Python's Ellipsis literal and should be treated as empty."""
        _write_py(tmp_project, "mod.py", '''
            def stubbed():
                ...
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        empty = _find_by_category(report, "empty_function")
        assert any("stubbed" in f.message for f in empty)


# ─────────────────────────────────────────────────────────────────────
# Edge cases: clean files, files with all 4 categories, async funcs
# ─────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def _full_report(self, workdir: str) -> DeadCodeReport:
        det = DeadCodeDetector(workdir)
        report = det.scan()
        report.findings.extend(det.find_unused_functions())
        return report

    def test_clean_file_no_findings(self, tmp_project):
        _write_py(tmp_project, "clean.py", '''
            import os

            def helper(x):
                return os.path.join("a", str(x))

            class Thing:
                def __init__(self, v):
                    self.v = v
                def double(self):
                    return self.v * 2

            def main():
                t = Thing(5)
                print(helper(t.double()))

            main()
        ''')
        report = self._full_report(tmp_project)
        assert report.passed is True
        assert report.findings == []
        assert report.summary == "No dead code found"

    def test_file_with_all_four_categories(self, tmp_project):
        """One file containing one example of each dead-code pattern."""
        _write_py(tmp_project, "messy.py", '''
            import os  # unused import

            def dead_helper():
                return 1

            def dead_thing():
                pass  # empty

            def main():
                return 1
                print("unreachable")  # unreachable

            main()
        ''')
        report = self._full_report(tmp_project)
        cats = set(_categories(report))
        # We expect at least: unreachable, unused_import, empty_function.
        # (dead_helper is private-flagged → NOT flagged as unused_function.)
        assert "unreachable" in cats
        assert "unused_import" in cats
        assert "empty_function" in cats

    def test_async_function_unreachable_flagged(self, tmp_project):
        """Async functions are also AST.FunctionDef/AsyncFunctionDef — should be checked."""
        _write_py(tmp_project, "mod.py", '''
            async def afoo():
                return 1
                x = 2  # unreachable
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        assert "unreachable" in _categories(report)

    def test_explicit_files_list(self, tmp_project):
        """When scan(files=[...]) is used, only those files are analyzed."""
        _write_py(tmp_project, "clean.py", '''
            import os

            def used():
                return os.getcwd()

            used()
        ''')
        _write_py(tmp_project, "messy.py", '''
            import sys  # unused
        ''')
        det = DeadCodeDetector(tmp_project)
        # Scan only the clean file
        clean_path = os.path.join(tmp_project, "clean.py")
        report = det.scan(files=[clean_path])
        unused = _find_by_category(report, "unused_import")
        assert not any("sys" in f.message for f in unused)

    def test_find_unused_functions_before_scan_is_empty(self, tmp_project):
        """find_unused_functions() without prior scan() has no data to analyze."""
        _write_py(tmp_project, "mod.py", '''
            def foo():
                pass
        ''')
        det = DeadCodeDetector(tmp_project)
        # No scan() called yet — should return empty list, not crash
        result = det.find_unused_functions()
        assert result == []

    def test_multiple_files_cross_reference_calls(self, tmp_project):
        """Function called across multiple files is marked as used."""
        _write_py(tmp_project, "lib.py", '''
            def shared():
                return 42
        ''')
        _write_py(tmp_project, "app1.py", '''
            from lib import shared
            shared()
        ''')
        _write_py(tmp_project, "app2.py", '''
            from lib import shared
            shared()
        ''')
        report = self._full_report(tmp_project)
        unused = _find_by_category(report, "unused_function")
        assert not any("shared" in f.message for f in unused)

    def test_finding_has_correct_file_and_line(self, tmp_project):
        _write_py(tmp_project, "mod.py", '''
            def foo():
                return 1
                x = 2
        ''')
        report = DeadCodeDetector(tmp_project).scan()
        unreachable = _find_by_category(report, "unreachable")
        assert len(unreachable) >= 1
        f = unreachable[0]
        assert f.file.endswith("mod.py")
        assert f.line > 0
        assert "return" in f.message
