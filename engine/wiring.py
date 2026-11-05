"""Wiring reachability check (GR-165): is everything defined actually reachable?

``engine/dead_code.py`` counts call sites. It cannot tell "called from a
mounted router" from "called from a dict nothing reads", and its whitelist
covers exactly the names where wiring bugs hide (main/run/handle/process/
execute/dispatch). This module answers the operator's question instead:

* what are this repo's entry points — console scripts, ``__main__`` guards,
  consumed dispatch tables, attribute registries like the MCP ``_tools``
  mapping, the evaluator's advertised tool list?
* which first-party definitions (functions, methods, classes) are reachable
  from them?
* which are NOT — and which of those are exemptions (the dead-code whitelist
  names and this repo's known-unwired set, REPORTED here, never silently
  dropped)?

Deliberate semantics
--------------------
* Def-level closure: reachability flows from an entry definition through the
  names its body loads, and a file's module scope is executed when any of its
  definitions is reachable. A name loaded ONLY inside an unreachable function
  (the handler constructed in a ``build_router`` nobody calls) therefore does
  NOT confer reachability — that is exactly the unmounted-handler shape.
* Bare-name matching: a name load reaches every definition of that bare name,
  in every module. Same-named methods reach each other — an
  over-approximation that trades recall for silence over false alarms, which
  is the right trade for a health check.
* Test suites are consumers: ``test_*`` functions and ``Test*`` classes under
  a ``tests`` directory are seeds (pytest invokes them by name), and so are
  ``pytest.fixture``-decorated functions (pytest injects them by parameter
  name) and ``pytest_*`` hooks in conftest.py. Production code exercised only
  through tests counts as wired. The four shapes this check targets — an
  unwired CLI command, an unmounted handler, an unregistered tool, a config
  path nobody calls — stay invisible to tests precisely because nothing
  wires them. http.server's ``do_<VERB>`` methods are seeds too (the stdlib
  dispatches them through ``getattr``).
* Attribute and name references inside a container literal confer
  reachability only when they occur inside a CONSUMED container (a
  dict/list/set/tuple assigned to a name that some reachable code loads —
  directly, or via ``{**BASE}`` spreads inside another registry). That is
  how attribute registries work: the handler refs in the MCP server's
  ``self._tools`` dict only matter because dispatch reads it. String members
  of a dict seed reachability ONLY through the dispatch-table rule (a
  module-level dict read by code that also resolves ``globals()[name]``) —
  a config/defaults dict whose keys collide with function names is not a
  dispatch table. A registry literal nothing loads is exactly the
  unmounted-handler bug, so its entries do NOT make their targets
  reachable — they become the finding's evidence.
* Registry/dispatch drift: a module that defines ``_tool_*`` methods and
  dispatches string names in ``==`` comparisons must advertise every
  dispatched name in a consumed registry. A dispatched-but-unadvertised tool
  can never be called by a client ("implemented but absent from the tool
  registry"). Only that direction is checked: registry strings also carry
  schema text, so the reverse direction would flag every advertised string
  with no method behind it.
* Scope: the distribution's own packages (from pyproject
  ``[tool.setuptools.packages.find]`` include patterns) plus ``tests/``, so
  vendored demo trees are never flagged. Fixture sub-trees under ``tests/``
  (``fixtures``, ``reliability``) are skipped and REPORTED as skipped paths
  — never silently suppressed.
* Python 3.10+ only: pyproject.toml is parsed with a line scan, not
  ``tomllib`` (same rule as scripts/check_docs_drift.py).

Exemptions come from two sources, both reported in the check detail: the
``WHITELIST_FUNCTIONS`` names from ``engine/dead_code.py`` (reused verbatim),
and ``REPO_EXEMPTIONS`` below — the definitions this repo knowingly carries
unwired, each with its reason. This module deliberately does not import
``engine.doctor`` (the dependency points the other way);
``doctor.check_wiring`` turns a report into a Check.
"""

from __future__ import annotations

import ast
import os
import re
from collections import deque
from dataclasses import dataclass, field

from engine.dead_code import DeadCodeDetector

WHITELIST_FUNCTIONS = DeadCodeDetector.WHITELIST_FUNCTIONS

# Never swept as first-party source, in any repo (fixtures and vendored junk).
_SKIP_DIRS = {
    ".git",
    ".gitreins",
    ".vfs",
    ".coding-hermes",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".memory-bank",
    "site-packages",
    ".github",
}
# Sub-trees under a tests directory that hold intentionally-broken fixture
# code. Skipped AND reported — the check detail names them.
_TEST_FIXTURE_DIRS = {"fixtures", "reliability"}
# dead_code's FRAMEWORK_DECORATORS, evaluated the same way: a def carrying
# one of these is invoked by a framework, not by a call site.
_FRAMEWORK_DECORATORS = {"pytest.fixture", "fixture"}


@dataclass
class WiringFinding:
    file: str
    line: int
    symbol: str
    reason: str

    def render(self) -> str:
        return f"{self.file}:{self.line} {self.symbol} — {self.reason}"


@dataclass
class WiringExemption:
    file: str
    line: int
    symbol: str
    name: str
    source: str = "dead_code.WHITELIST_FUNCTIONS"

    def render(self) -> str:
        return f"{self.symbol} ({self.name}) {self.file}:{self.line}"


@dataclass
class WiringReport:
    entry_points: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    modules_scanned: int = 0
    findings: list[WiringFinding] = field(default_factory=list)
    exemptions: list[WiringExemption] = field(default_factory=list)


@dataclass
class _Def:
    file: str
    line: int
    name: str  # bare name
    qual: str  # module.path:Class.method for reporting
    node: ast.AST | None = None  # the FunctionDef — for config-key enrichment
    framework_decorated: bool = False
    is_method: bool = False


@dataclass
class _Container:
    """A dict/list/set/tuple literal assigned to a name (a candidate registry)."""

    var: str
    file: str
    line: int
    strings: set[str] = field(default_factory=set)
    attrs: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    # Only module/class-level registries seed entry points (a dispatch table);
    # function-local ones (the MCP server builds ``self._tools`` in
    # ``__init__``) still carry conditional references.
    module_level: bool = False
    # Only dict literals seed dispatch tables; a set of whitelisted names or
    # a defaults dict is a container, not a command table.
    is_dict: bool = False


@dataclass
class _FileFacts:
    file: str
    defs: list[_Def] = field(default_factory=list)
    classes: list[tuple[str, int]] = field(default_factory=list)  # (name, line)
    # Names loaded at module scope (ownerless) — executed on import.
    module_loads: set[str] = field(default_factory=set)
    # Names loaded inside a def body, keyed by that def node's id.
    body_refs: dict[int, set[str]] = field(default_factory=dict)
    # Every load site of a name: (file, line, owner id; 0 = module scope).
    load_sites: dict[str, list[tuple[str, int, int]]] = field(default_factory=dict)
    containers: list[_Container] = field(default_factory=list)
    dispatched: set[str] = field(default_factory=set)  # strings compared with ==
    has_main_guard: bool = False


def _container_target_name(targets: list[ast.expr]) -> str:
    """The registry's variable name: first Name target, else Attribute attr."""
    for target in targets:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    return element.id
    return ""


def _is_registry_value(value: ast.AST) -> bool:
    return isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple))


def _is_framework_decorated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for ``pytest.fixture`` — including ``@pytest.fixture(autouse=True)``."""
    for decorator in node.decorator_list:
        name = ast.unparse(decorator).split("(", 1)[0].strip()
        if name in _FRAMEWORK_DECORATORS or name.rsplit(".", 1)[-1] == "fixture":
            return True
    return False


class _ModuleScanner:
    """One recursive pass over a module, collecting defs/loads/registries.

    Every statement kind is visited, and loads are attributed to their owner:
    module scope (executed on import) or an enclosing def body (executed only
    if that def is reachable). The first cut of this check inspected only
    Assign/If nodes and missed every load inside Return/Expr/Call and every
    ``from x import y`` alias — an under-count so severe that most of a
    healthy tree read as unwired. Registry literals are tracked on the value
    side of Assign/AnnAssign; their members only confer reachability when the
    registry's name is loaded somewhere.
    """

    def __init__(self, rel: str, module_qual: str) -> None:
        self.facts = _FileFacts(file=rel)
        self.rel = rel
        self.module_qual = module_qual
        self.function_depth = 0
        self._owner_stack: list[int] = []
        self._method_class: dict[int, str] = {}

    def scan(self, tree: ast.Module) -> _FileFacts:
        # Direct method children, so a method is registered exactly once and
        # carries its class name (ast.walk would visit it twice).
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._method_class[id(child)] = node.name
        self._visit(tree, None)
        return self.facts

    def _add_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        class_name = self._method_class.get(id(node))
        qual = (
            f"{self.module_qual}:{class_name}.{node.name}"
            if class_name
            else f"{self.module_qual}:{node.name}"
        )
        self.facts.defs.append(
            _Def(
                file=self.rel,
                line=node.lineno,
                name=node.name,
                qual=qual,
                node=node,
                framework_decorated=_is_framework_decorated(node),
                is_method=class_name is not None,
            )
        )

    def _record_load(self, name: str, line: int) -> None:
        owner = self._owner_stack[-1] if self._owner_stack else 0
        self.facts.load_sites.setdefault(name, []).append((self.rel, line, owner))
        if owner == 0:
            self.facts.module_loads.add(name)
        else:
            self.facts.body_refs.setdefault(owner, set()).add(name)

    def _visit(self, node: ast.AST, container: _Container | None) -> None:
        if isinstance(node, ast.Constant):
            if container is not None and isinstance(node.value, str):
                container.strings.add(node.value)
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if container is not None:
                    container.names.add(node.id)
                else:
                    self._record_load(node.id, node.lineno)
            return
        if isinstance(node, ast.Attribute):
            if container is not None:
                container.attrs.add(node.attr)
            else:
                self._record_load(node.attr, node.lineno)
            # The value side is walked too (obj.method still loads obj).
            self._visit(node.value, container)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._record_load(alias.asname or alias.name.split(".")[0], node.lineno)
            return
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    self._record_load(alias.asname or alias.name, node.lineno)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._add_def(node)
            # Decorators, defaults and annotations execute at definition time
            # (the enclosing scope), so they load with the OUTER owner.
            for decorator in node.decorator_list:
                self._visit(decorator, None)
            self._visit(node.args, None)
            if node.returns is not None:
                self._visit(node.returns, None)
            self.function_depth += 1
            self._owner_stack.append(id(node))
            for stmt in node.body:
                self._visit(stmt, None)
            self._owner_stack.pop()
            self.function_depth -= 1
            return
        if isinstance(node, ast.Lambda):
            # Parameters are bindings, not loads; the body loads with the
            # enclosing owner (a module-level lambda runs at import).
            self._visit(node.body, container)
            return
        if isinstance(node, ast.ClassDef):
            self.facts.classes.append((node.name, node.lineno))
            self.facts.defs.append(
                _Def(
                    file=self.rel,
                    line=node.lineno,
                    name=node.name,
                    qual=f"{self.module_qual}:{node.name}",
                    node=node,
                )
            )
            for decorator in node.decorator_list:
                self._visit(decorator, None)
            for base in node.bases:
                self._visit(base, None)
            self._owner_stack.append(id(node))
            for stmt in node.body:
                self._visit(stmt, None)
            self._owner_stack.pop()
            return
        if isinstance(node, ast.Assign):
            registry = _is_registry_value(node.value)
            var = _container_target_name(node.targets) if registry else ""
            inner: _Container | None = None
            if registry and var:
                inner = _Container(
                    var=var,
                    file=self.rel,
                    line=node.lineno,
                    module_level=self.function_depth == 0,
                    is_dict=isinstance(node.value, ast.Dict),
                )
                self.facts.containers.append(inner)
            for target in node.targets:
                self._visit(target, None)
            self._visit(node.value, inner)
            return
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            registry = _is_registry_value(node.value)
            var = _container_target_name([node.target]) if registry else ""
            inner = None
            if registry and var:
                inner = _Container(
                    var=var,
                    file=self.rel,
                    line=node.lineno,
                    module_level=self.function_depth == 0,
                    is_dict=isinstance(node.value, ast.Dict),
                )
                self.facts.containers.append(inner)
            self._visit(node.target, None)
            self._visit(node.value, inner)
            return
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                for comparator in test.comparators:
                    if isinstance(comparator, ast.Constant) and comparator.value == "__main__":
                        self.facts.has_main_guard = True
            self._visit(test, None)
            for stmt in node.body:
                self._visit(stmt, None)
            for stmt in node.orelse:
                self._visit(stmt, None)
            return
        if isinstance(node, ast.Compare):
            # `x == "name"` comparisons: the dispatched-name surface (the
            # evaluator dispatches tools by comparing the call's name).
            if any(isinstance(op, ast.Eq) for op in node.ops):
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        self.facts.dispatched.add(comparator.value)
            for child in ast.iter_child_nodes(node):
                self._visit(child, container)
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, container)


def _scan_file(rel: str, path: str) -> _FileFacts | None:
    """AST-scan one file; None when it cannot be read or parsed."""
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None
    module_qual = rel[:-3].replace("/", ".")
    return _ModuleScanner(rel, module_qual).scan(tree)


def _scope_files(workdir: str) -> tuple[list[str], list[str]]:
    """(files to scan, reported-skip paths) — distribution packages + tests/."""
    root = os.path.abspath(workdir)
    files: list[str] = []
    skipped: list[str] = []
    package_dirs: list[str] = []
    pyproject = os.path.join(root, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = ""
        include = re.search(r"include\s*=\s*\[([^\]]*)\]", text)
        if include:
            for raw in include.group(1).split(","):
                pattern = raw.strip().strip('"').strip("'").rstrip("*")
                if pattern and os.path.isdir(os.path.join(root, pattern)):
                    package_dirs.append(pattern)
    if package_dirs:
        roots = package_dirs
        if os.path.isdir(os.path.join(root, "tests")):
            roots.append("tests")
    else:
        roots = [""]

    for sub in roots:
        base = os.path.join(root, sub)
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            if rel_dir == "tests":
                for fixture in _TEST_FIXTURE_DIRS:
                    if fixture in dirnames:
                        dirnames.remove(fixture)
                        skipped.append(os.path.join("tests", fixture))
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    rel = os.path.normpath(os.path.join(rel_dir, filename))
                    files.append(rel.replace(os.sep, "/"))
    return files, skipped


def _pyproject_entry_points(workdir: str) -> list[tuple[str, str]]:
    """[(module, func)] from [project.scripts] — line scan, 3.10-safe."""
    path = os.path.join(workdir, "pyproject.toml")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    section: str | None = None
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
            continue
        if section == "project.scripts":
            match = re.match(r'^[^\s=]+\s*=\s*"([^:"]+):([^"]+)"\s*$', stripped)
            if match:
                entries.append((match.group(1), match.group(2)))
    return entries


# _TEST_FIXTURE_DIRS are skipped and reported; these are the definitions the
# scan DOES see and that this repo carries as known-unwired, kept out of the
# findings on purpose. Each entry is reported as a repo-specific exemption in
# the check detail — this table is the "why", not a silent suppression.
# (Methods of reachable classes are treated as wired-by-class and never
# produce findings, so only module-level functions and unreachable classes
# can ever need an entry here.)
REPO_EXEMPTIONS: dict[str, str] = {
    # engine/config_schema.py:format_issues — module-level function with zero
    # references in the tree (config-validate formats inline). Pre-existing
    # dead code; removing it belongs to a cleanup row, not to the check that
    # found it.
    "engine.config_schema:format_issues": "pre-existing dead code, removal tracked separately",
    # engine/pipeline.py:_normalize_yaml_bool_keys — recursive walker whose
    # only references are its own (pipeline.py:1593/1596); the caller that was
    # meant to invoke it on pipeline YAML never landed. Pre-existing.
    "engine.pipeline:_normalize_yaml_bool_keys": (
        "pre-existing dead code, removal tracked separately"
    ),
    # engine/data_protection.py:policy_from_config — module-level function,
    # zero references in the tree. Same pre-existing case.
    "engine.data_protection:policy_from_config": (
        "pre-existing dead code, removal tracked separately"
    ),
    # tests/test_scanner_nice.py:scaner_defaults — test helper with zero
    # references (name typo class). Same pre-existing case, in test code.
    "tests.test_scanner_nice:scaner_defaults": "pre-existing dead test helper",
}


def _reachable_scope_loads(
    facts_by_file: dict[str, _FileFacts],
    reach_defs: set[int],
    processed_files: set[str],
    optimistic: bool,
) -> dict[str, set[str]]:
    """Per-file loads reachable scope can execute (all loads, when optimistic)."""
    out: dict[str, set[str]] = {}
    for rel, facts in facts_by_file.items():
        loads = set(facts.module_loads) if optimistic or rel in processed_files else set()
        for defn in facts.defs:
            if optimistic or id(defn) in reach_defs:
                loads |= facts.body_refs.get(id(defn.node), set())
        out[rel] = loads
    return out


def _entry_points(
    facts_by_file: dict[str, _FileFacts],
    defs: list[_Def],
    workdir: str,
    consumed_names: set[str],
    reachable_loads: dict[str, set[str]],
) -> tuple[set[str], list[str]]:
    """(entry names, lines) from console scripts, guards, tables, framework hooks."""
    entry_names: set[str] = set()
    lines: list[str] = []
    module_of = {rel[:-3].replace("/", "."): rel for rel in facts_by_file}
    for module, func in _pyproject_entry_points(workdir):
        if module_of.get(module) is not None:
            entry_names.add(func)
            lines.append(f"console script {module}:{func}")
    for rel, facts in facts_by_file.items():
        if facts.has_main_guard and any(d.name == "main" for d in facts.defs):
            entry_names.add("main")
            lines.append(f"__main__ guard in {rel}")
        verb_handlers = sorted(
            d.name for d in facts.defs if d.name.startswith("do_") and d.is_method
        )
        if verb_handlers:
            # http.server dispatches do_<VERB> through getattr on the handler
            # class — unreachable by name-based closure, wired by contract.
            entry_names.update(verb_handlers)
            lines.append(f"http.server verb handler(s) ({len(verb_handlers)}) in {rel}")
        pytest_hooks = sorted(
            d.name
            for d in facts.defs
            if d.name.startswith("pytest_") and rel.endswith("conftest.py")
        )
        if pytest_hooks:
            # pytest plugin hooks are invoked by name inside conftest.py.
            entry_names.update(pytest_hooks)
            lines.append(f"pytest plugin hook(s) ({len(pytest_hooks)}) in {rel}")
        uses_globals = "globals" in reachable_loads.get(rel, set())
        for container in facts.containers:
            if container.var not in consumed_names or not container.module_level:
                continue
            if not container.is_dict or not uses_globals:
                # A defaults/config dict whose keys collide with function
                # names is not a dispatch table: string seeding requires the
                # ``globals()[...]`` resolution the CLI dispatch actually
                # performs (without it, table entries stay ordinary
                # conditional references).
                continue
            wired = sorted(
                name
                for name in container.strings
                if any(d.file == rel and d.name == name for d in facts.defs)
            )
            prefixed = sorted(
                f"cmd_{name}"
                for name in container.strings
                if any(d.file == rel and d.name == f"cmd_{name}" for d in facts.defs)
            )
            if wired or prefixed:
                entry_names.update(wired)
                entry_names.update(prefixed)
                lines.append(
                    f"dispatch table {container.var} ({len(wired) + len(prefixed)} handler(s)) "
                    f"in {rel}"
                )
    test_defs = [
        d
        for d in defs
        if (d.name.startswith("test_") or d.framework_decorated)
        and (d.file == "tests" or d.file.startswith("tests/"))
    ]
    test_classes: list[tuple[str, int]] = []
    for rel, facts in facts_by_file.items():
        if rel != "tests" and not rel.startswith("tests/"):
            continue
        test_classes.extend((name, line) for name, line in facts.classes if name.startswith("Test"))
    if test_defs:
        entry_names.update(d.name for d in test_defs)
        lines.append(f"pytest ({len(test_defs)} test function(s) in tests/)")
    if test_classes:
        entry_names.update(name for name, _line in test_classes)
        lines.append(f"pytest ({len(test_classes)} test class(es) in tests/)")
    return entry_names, lines


def _closure(
    facts_by_file: dict[str, _FileFacts],
    defs_by_name: dict[str, list[_Def]],
    methods_of_class: dict[str, list[_Def]],
    entry_names: set[str],
    consumed_names: set[str],
) -> tuple[set[int], set[str]]:
    """Def-level reachability closure; returns (reach def ids, processed files).

    A file's module scope is executed when any of its definitions is
    reachable; a reachable class carries its methods (they hang off a wired
    object); everything else flows through bare-name loads from reachable
    scope and consumed-registry members.
    """
    refs_by_file: dict[str, set[str]] = {}
    for rel, facts in facts_by_file.items():
        refs = set(facts.module_loads)
        for container in facts.containers:
            if container.var in consumed_names:
                # Attribute and name members are real references (the MCP
                # ``_tools`` mapping, ``{**BASE}`` spreads). String members do
                # NOT fold here: a config dict's keys are not handler names —
                # strings seed only through the dispatch-table rule above.
                refs |= container.attrs | container.names
        refs_by_file[rel] = refs

    reach_defs: set[int] = set()
    processed_files: set[str] = set()
    work: deque[_Def] = deque()

    def _mark(defn: _Def) -> None:
        if id(defn) not in reach_defs:
            reach_defs.add(id(defn))
            work.append(defn)

    all_defs = [d for ds in defs_by_name.values() for d in ds]
    for defn in all_defs:
        if defn.name in entry_names or defn.framework_decorated:
            _mark(defn)
    while work:
        defn = work.popleft()
        facts = facts_by_file[defn.file]
        if defn.file not in processed_files:
            processed_files.add(defn.file)
            for name in refs_by_file.get(defn.file, ()):
                for target in defs_by_name.get(name, ()):
                    _mark(target)
        body_names = facts.body_refs.get(id(defn.node), set()) if defn.node is not None else set()
        for name in body_names:
            for target in defs_by_name.get(name, ()):
                _mark(target)
        for method in methods_of_class.get(defn.qual, ()):
            _mark(method)
    return reach_defs, processed_files


def scan(workdir: str) -> WiringReport:
    """Run the wiring analysis; pure and read-only."""
    report = WiringReport()
    root = os.path.abspath(workdir)
    rels, skipped = _scope_files(workdir)
    report.skipped_paths = sorted(skipped)
    facts_by_file: dict[str, _FileFacts] = {}
    for rel in rels:
        facts = _scan_file(rel, os.path.join(root, *rel.split("/")))
        if facts is not None:
            facts_by_file[rel] = facts
    report.modules_scanned = len(facts_by_file)
    if not facts_by_file:
        return report

    # Consumption is closure-aware and runs in two passes: pass 1 is
    # optimistic (any load anywhere consumes its registry), pass 2 keeps only
    # the loads reachable scope can actually execute — module scope of files
    # that hold reachable defs, plus bodies of reachable defs. A registry
    # whose ONLY reader is an unreachable function ends up unconsumed in pass
    # 2, which is precisely the "registry literal that no reachable code
    # consumes" shape.
    all_loads: set[str] = set()
    for facts in facts_by_file.values():
        all_loads |= facts.module_loads
        for names in facts.body_refs.values():
            all_loads |= names
    container_members: set[str] = set()
    for facts in facts_by_file.values():
        for container in facts.containers:
            container_members |= container.names | container.attrs

    defs: list[_Def] = []
    for facts in facts_by_file.values():
        defs.extend(facts.defs)
    defs_by_name: dict[str, list[_Def]] = {}
    for defn in defs:
        defs_by_name.setdefault(defn.name, []).append(defn)
    # Class → its direct methods (qual prefix "module:Class.").
    methods_of_class: dict[str, list[_Def]] = {}
    for defn in defs:
        if defn.is_method and "." in defn.qual.split(":", 1)[1]:
            class_qual = defn.qual.rsplit(".", 1)[0]
            methods_of_class.setdefault(class_qual, []).append(defn)

    reach_defs: set[int] = set()  # id(_Def)
    processed_files: set[str] = set()
    consumed_names: set[str] = all_loads | container_members
    entry_names: set[str] = set()
    entry_lines: list[str] = []
    # Shrink-to-fixpoint: consumption narrows to what reachable scope can
    # execute, which can demote defs, which narrows consumption further. The
    # sequence is monotone (every input set only shrinks), so it terminates.
    for _iteration in range(25):
        reachable_loads = _reachable_scope_loads(
            facts_by_file, reach_defs, processed_files, optimistic=(_iteration == 0)
        )
        entry_names, entry_lines = _entry_points(
            facts_by_file, defs, workdir, consumed_names, reachable_loads
        )
        new_reach, new_files = _closure(
            facts_by_file, defs_by_name, methods_of_class, entry_names, consumed_names
        )
        stable = new_reach == reach_defs
        reach_defs, processed_files = new_reach, new_files
        merged: set[str] = set(container_members)
        for loads in _reachable_scope_loads(
            facts_by_file, reach_defs, processed_files, optimistic=False
        ).values():
            merged |= loads
        consumed_names = merged
        if stable:
            break
    report.entry_points = entry_lines

    # ── Findings ─────────────────────────────────────────────────────
    sites_by_name: dict[str, list[tuple[str, int]]] = {}
    for facts in facts_by_file.values():
        for name, sites in facts.load_sites.items():
            sites_by_name.setdefault(name, []).extend(
                (site_file, site_line) for site_file, site_line, _owner in sites
            )
    lit_refs: dict[str, list[tuple[str, int, str]]] = {}
    consumed_refs: dict[str, list[tuple[str, int]]] = {}
    lit_strings: dict[str, list[tuple[str, int, str]]] = {}
    for facts in facts_by_file.values():
        for container in facts.containers:
            # String members are not references (see _closure); only
            # attribute/name members of a registry say anything about wiring.
            targets = container.attrs | container.names
            for name in targets:
                if container.var in consumed_names:
                    consumed_refs.setdefault(name, []).append((container.file, container.line))
                else:
                    lit_refs.setdefault(name, []).append(
                        (container.file, container.line, container.var)
                    )
            if container.var not in consumed_names:
                for name in container.strings:
                    lit_strings.setdefault(name, []).append(
                        (container.file, container.line, container.var)
                    )
    for defn in defs:
        if id(defn) in reach_defs or defn.framework_decorated:
            continue
        if defn.qual in REPO_EXEMPTIONS:
            report.exemptions.append(
                WiringExemption(
                    file=defn.file,
                    line=defn.line,
                    symbol=defn.qual,
                    name=defn.name,
                    source=f"repo-specific: {REPO_EXEMPTIONS[defn.qual]}",
                )
            )
            continue
        if defn.name in WHITELIST_FUNCTIONS:
            report.exemptions.append(
                WiringExemption(file=defn.file, line=defn.line, symbol=defn.qual, name=defn.name)
            )
            continue
        reason = (
            "defined but not reachable from any entry point (no references outside its definition)"
        )
        if defn.name in consumed_refs:
            reason = "referenced only from unreachable code: " + ", ".join(
                f"{site[0]}:{site[1]}" for site in consumed_refs[defn.name][:3]
            )
        elif defn.name in lit_refs:
            lit_file, lit_line, lit_var = lit_refs[defn.name][0]
            reason = (
                f"only referenced inside registry literal {lit_var} ({lit_file}:{lit_line}) "
                "that no reachable code consumes"
            )
        elif defn.name in sites_by_name:
            reason = "referenced only from unreachable code: " + ", ".join(
                f"{site[0]}:{site[1]}" for site in sites_by_name[defn.name][:3]
            )
        if defn.name in lit_strings:
            lit_file, lit_line, lit_var = lit_strings[defn.name][0]
            reason += (
                f"; name appears in unconsumed registry literal {lit_var} ({lit_file}:{lit_line})"
            )
        keys = _config_keys_read(defn.node)
        if keys:
            reason += f"; reads keyed config: {', '.join(keys)}"
        report.findings.append(
            WiringFinding(file=defn.file, line=defn.line, symbol=defn.qual, reason=reason)
        )

    # ── Registry/dispatch drift (evaluator-shaped modules) ───────────
    # A module that defines ``_tool_*`` methods and dispatches tool names by
    # string comparison must advertise every dispatched name in a consumed
    # registry — a dispatched-but-unadvertised tool can never be called by a
    # client (the "implemented but absent from the tool registry" shape).
    # Only that direction is checked: registry strings also carry schema
    # text (descriptions, parameter names), so the reverse direction would
    # flag every advertised string with no method behind it.
    for rel, facts in facts_by_file.items():
        tool_methods = {
            defn.name[len("_tool_") :]: defn
            for defn in facts.defs
            if defn.name.startswith("_tool_")
        }
        if not tool_methods:
            continue
        advertised: set[str] = set()
        registry_line = 0
        for container in facts.containers:
            if container.var in consumed_names:
                advertised |= container.strings
                registry_line = registry_line or container.line
        for tool, defn in sorted(tool_methods.items()):
            if tool in facts.dispatched and advertised and tool not in advertised:
                # Prefer the drift reason over the generic unreachable one
                # already emitted for the same method.
                report.findings = [
                    f for f in report.findings if not (f.file == rel and f.symbol == defn.qual)
                ]
                report.findings.append(
                    WiringFinding(
                        file=rel,
                        line=defn.line,
                        symbol=defn.qual,
                        reason=(
                            f"tool '{tool}' is dispatched but absent from every consumed "
                            f"registry in {rel} (registry at line {registry_line})"
                        ),
                    )
                )

    report.findings.sort(key=lambda f: (f.file, f.line))
    return report


def _config_keys_read(node: ast.AST | None) -> list[str]:
    """Constant subscript keys inside an unreachable def (config readers)."""
    if node is None:
        return []
    keys: list[str] = []
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.slice, ast.Constant)
            and isinstance(sub.slice.value, str)
        ):
            keys.append(sub.slice.value)
    return sorted(set(keys))


def summarize(report: WiringReport, strict: bool = False) -> tuple[str, str, str]:
    """(status, detail, hint) for doctor's Check — statuses ok/warn/fail."""
    skipped_note = ""
    if report.skipped_paths:
        skipped_note = f"; skipped fixture trees: {', '.join(report.skipped_paths)}"
    if report.modules_scanned == 0:
        return ("skip", "no first-party Python sources found — wiring not assessed", "")
    if report.findings:
        shown = [f.render() for f in report.findings[:3]]
        more = f" … and {len(report.findings) - 3} more" if len(report.findings) > 3 else ""
        detail = (
            f"{len(report.findings)} unwired finding(s) from {len(report.entry_points)} "
            f"entry point(s): {'; '.join(shown)}{more}. "
            f"{len(report.exemptions)} exemption(s) reported{skipped_note}"
        )
        hint = (
            "wire the symbol into its dispatch table/registry or remove it; "
            "`--json` lists every finding"
        )
        if strict:
            hint = "strict mode: wiring findings fail doctor. " + hint
        return ("fail" if strict else "warn", detail, hint)
    detail = (
        f"all definitions reachable from {len(report.entry_points)} entry point(s) across "
        f"{report.modules_scanned} module(s)"
    )
    if report.exemptions:
        names = ", ".join(e.symbol for e in report.exemptions[:5])
        more = f" … and {len(report.exemptions) - 5} more" if len(report.exemptions) > 5 else ""
        detail += f"; {len(report.exemptions)} exemption(s) reported: {names}{more}"
    if skipped_note:
        detail += skipped_note
    return ("ok", detail, "")
