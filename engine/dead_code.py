"""Dead Code Detector — AST-based static analysis for unreachable and unused code.

Catches:
1. Unreachable code — statements after return/raise/break/continue in same block
2. Unused functions — defined but never called anywhere in the project
3. Unused imports — modules imported but never referenced
4. Empty functions — defined with pass/... only, no body
"""

import ast
import os
from dataclasses import dataclass, field


@dataclass
class DeadCodeFinding:
    file: str
    line: int
    category: str  # unreachable | unused_function | unused_import | empty_function
    message: str


@dataclass
class DeadCodeReport:
    findings: list[DeadCodeFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.findings) == 0

    @property
    def summary(self) -> str:
        if not self.findings:
            return "No dead code found"
        lines = []
        by_cat: dict[str, list[DeadCodeFinding]] = {}
        for f in self.findings:
            by_cat.setdefault(f.category, []).append(f)
        for cat, finds in sorted(by_cat.items()):
            lines.append(f"\n  {cat.upper()} ({len(finds)}):")
            for f in finds[:10]:
                lines.append(f"    {f.file}:{f.line} — {f.message}")
            if len(finds) > 10:
                lines.append(f"    ... and {len(finds) - 10} more")
        return "\n".join(lines)


class DeadCodeDetector:
    """AST-based dead code analysis for Python projects."""

    WHITELIST_FUNCTIONS = {
        # Standard dunder methods
        "__init__", "__repr__", "__str__", "__eq__", "__hash__", "__lt__",
        "__le__", "__gt__", "__ge__", "__add__", "__sub__", "__mul__",
        "__call__", "__getitem__", "__setitem__", "__delitem__",
        "__enter__", "__exit__", "__iter__", "__next__", "__len__",
        "__contains__", "__getattr__", "__setattr__", "__delattr__",
        "__post_init__", "__new__",
        # Test functions
        "setUp", "tearDown", "setUpClass", "tearDownClass",
        # Common framework hooks
        "main", "run", "handle", "process", "execute", "dispatch",
    }
    # Decorators that mean a function IS called (just not via Call AST node)
    CALLED_VIA_DECORATOR = {"property", "cached_property", "staticmethod", "classmethod"}
    # Decorator qualifiers that mark fixture/test functions called by frameworks
    FRAMEWORK_DECORATORS = {"pytest.fixture", "fixture"}

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self._func_defs: dict[str, list[tuple[str, int]]] = {}  # func_name -> [(file, line)]
        self._func_calls: set[str] = set()
        self._imports: dict[str, set[str]] = {}  # file -> {import names}

    def scan(self, files: list[str] | None = None) -> DeadCodeReport:
        """Scan project for dead code. If files is None, scans all Python files."""
        report = DeadCodeReport()

        if files is None:
            files = self._find_python_files()

        # Phase 1: Build symbol table (definitions, imports)
        for fpath in files:
            self._collect_symbols(fpath)

        # Phase 2: Collect all function calls across the project
        for fpath in files:
            self._collect_calls(fpath)

        # Phase 3: Detect dead code per file
        for fpath in files:
            findings = self._analyze_file(fpath)
            report.findings.extend(findings)

        return report

    def _find_python_files(self) -> list[str]:
        """Find all Python files in the project (excluding venv, node_modules, etc.)."""
        py_files = []
        skip_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules",
                      ".tox", ".eggs", "build", "dist", ".pytest_cache",
                      ".gitreins", "temporal-vector"}
        for root, dirs, filenames in os.walk(self.workdir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in filenames:
                if fname.endswith(".py"):
                    py_files.append(os.path.join(root, fname))
        return py_files

    def _relpath(self, abspath: str) -> str:
        try:
            return os.path.relpath(abspath, self.workdir)
        except ValueError:
            return abspath

    def _collect_symbols(self, fpath: str) -> None:
        """Collect function definitions and imports from a file."""
        try:
            with open(fpath, "r") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError, PermissionError):
            return

        rel = self._relpath(fpath)

        for node in ast.walk(tree):
            # Function definitions
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                if name not in self._func_defs:
                    self._func_defs[name] = []
                self._func_defs[name].append((rel, node.lineno))
                # Functions decorated with @property, @staticmethod etc. are
                # accessed via attribute access, not Call AST — treat as "called"
                for decorator in node.decorator_list:
                    dec_name = None
                    if isinstance(decorator, ast.Name):
                        dec_name = decorator.id
                    elif isinstance(decorator, ast.Attribute):
                        dec_name = decorator.attr
                        # Check for pytest.fixture style
                        if isinstance(decorator.value, ast.Name):
                            qualified = f"{decorator.value.id}.{dec_name}"
                            if qualified in self.FRAMEWORK_DECORATORS:
                                self._func_calls.add(name)
                                break
                    if dec_name and dec_name in self.CALLED_VIA_DECORATOR:
                        self._func_calls.add(name)
                        break
                    if dec_name in self.FRAMEWORK_DECORATORS:
                        self._func_calls.add(name)
                        break

            # Imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name
                    self._imports.setdefault(rel, set()).add(name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name != "*":
                        self._imports.setdefault(rel, set()).add(name)

    def _collect_calls(self, fpath: str) -> None:
        """Collect all function calls across the project."""
        try:
            with open(fpath, "r") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError, PermissionError):
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self._func_calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    self._func_calls.add(node.func.attr)

    def _analyze_file(self, fpath: str) -> list[DeadCodeFinding]:
        """Analyze a single file for dead code."""
        findings: list[DeadCodeFinding] = []
        try:
            with open(fpath, "r") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError, PermissionError):
            return findings

        rel = self._relpath(fpath)

        # --- UNREACHABLE CODE (function bodies only) ---
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            for i, child in enumerate(body):
                if isinstance(child, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                    if i + 1 < len(body):
                        next_sib = body[i + 1]
                        if isinstance(next_sib, ast.Expr) and isinstance(next_sib.value, ast.Constant):
                            continue  # Skip docstrings
                        findings.append(DeadCodeFinding(
                            file=rel, line=next_sib.lineno,
                            category="unreachable",
                            message=f"Code after {type(child).__name__.lower()} on line {child.lineno} is unreachable",
                        ))

        # --- EMPTY FUNCTIONS ---
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                # Strip docstrings
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    body = body[1:]
                if len(body) == 0 or (len(body) == 1 and isinstance(body[0], ast.Pass)):
                    findings.append(DeadCodeFinding(
                        file=rel, line=node.lineno,
                        category="empty_function",
                        message=f"Function '{node.name}' has no implementation (empty body)",
                    ))

        # --- UNUSED IMPORTS ---
        if rel in self._imports:
            used_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used_names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    if isinstance(node.value, ast.Name):
                        used_names.add(node.value.id)

            for imp_name in self._imports[rel]:
                # Split dotted imports to check root
                root = imp_name.split(".")[0]
                if root not in used_names and imp_name not in used_names:
                    # Find the import line
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                name = alias.asname or alias.name
                                if name == imp_name:
                                    findings.append(DeadCodeFinding(
                                        file=rel, line=node.lineno,
                                        category="unused_import",
                                        message=f"Import '{imp_name}' is never used",
                                    ))
                        elif isinstance(node, ast.ImportFrom):
                            for alias in node.names:
                                name = alias.asname or alias.name
                                if name == imp_name:
                                    findings.append(DeadCodeFinding(
                                        file=rel, line=node.lineno,
                                        category="unused_import",
                                        message=f"Import '{imp_name}' is never used",
                                    ))

        return findings

    def find_unused_functions(self) -> list[DeadCodeFinding]:
        """Post-scan: identify functions defined but never called project-wide."""
        findings: list[DeadCodeFinding] = []
        for func_name, defs in self._func_defs.items():
            if func_name.startswith("_"):
                continue  # Private functions are often unused by design
            if func_name.startswith("test_"):
                continue  # Test functions are called by pytest, not other code
            if func_name in self.WHITELIST_FUNCTIONS:
                continue
            if func_name not in self._func_calls:
                for file, line in defs:
                    findings.append(DeadCodeFinding(
                        file=file, line=line,
                        category="unused_function",
                        message=f"Function '{func_name}' is defined but never called in the project",
                    ))
        return findings
