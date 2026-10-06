#!/usr/bin/env python3
"""Inventory Python test files under tests/ and emit a heuristic class label.

The classifier is intentionally static: it parses source and never imports or
executes test modules. Heuristics are ordered by specificity; each rationale
records the strongest signals that selected the label.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "classes.json"
CLASSES = {"unit", "integration", "e2e", "contract", "wiring", "mutation"}


def _source_facts(path: Path) -> tuple[str, set[str], set[str], set[str]]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, set(), set(), set()

    imports: set[str] = set()
    functions: set[str] = set()
    fixtures: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                functions.add(node.name.lower())
            for arg in node.args.args + node.args.kwonlyargs:
                fixtures.add(arg.arg.lower())
            for decorator in node.decorator_list:
                text = ast.unparse(decorator).lower()
                if "parametrize" in text or "given(" in text or "@given" in text:
                    fixtures.add("__parameterized__")
    return source, imports, functions, fixtures


def classify(path: Path) -> dict[str, str]:
    source, imports, functions, fixtures = _source_facts(path)
    text = source.lower()
    names = " ".join(functions) + " " + path.stem.lower()
    evidence: list[str] = []

    explicit = next(
        (
            kind
            for kind in ("e2e", "contract", "integration", "wiring", "unit", "mutation")
            if re.search(rf"(?:^|[_-]){kind}(?:$|[_-])", names)
        ),
        None,
    )
    if explicit:
        label = explicit
        evidence.append(f"test/file name explicitly marks {label}")
    elif fixtures & {"__parameterized__"} and (
        "hypothesis" in imports
        or re.search(r"parametrize\s*\([^\n]{0,300},[^\n]{0,300},[^\n]{0,300},", text)
    ):
        label = "mutation"
        evidence.append("property-based or multi-case parametrized tests")
    elif "cli_runner" in fixtures or re.search(
        r"(?:subprocess\.(?:run|popen|check_output)|run_cli\s*\(|testclient\s*\(|test_client\s*\()",
        text,
    ):
        label = "e2e"
        evidence.append("invokes a CLI, subprocess, or user-facing client")
    elif any(
        token in text
        for token in ("jsonschema", "json schema", "openapi", "schema_path", "assert_schema")
    ) or re.search(r"assert\s+(?:set\([^\n]+\)|\w+\.keys\(\))", text):
        label = "contract"
        evidence.append("checks a schema or externally visible data shape")
    elif "mcp_client" in fixtures or (
        "mcp" in text and any(x in text for x in ("stdio", "json-rpc", "jsonrpc", "initialize"))
    ):
        label = "integration"
        evidence.append("exercises MCP/client interaction across components")
    elif "tmp_path" in fixtures and any(
        x in text for x in ("write_text(", "write_bytes(", "open(", "mkdir(", "temporarydirectory")
    ):
        label = "integration"
        evidence.append("uses temporary real files/directories as collaborating components")
    elif any(
        x in text
        for x in (
            "importlib.util.find_spec",
            "entry_points(",
            "entry_points.",
            "__main__",
            "console_scripts",
        )
    ) or re.search(r"assert\s+.+(?:is not none|exists\(\))", text):
        label = "wiring"
        evidence.append("checks imports, configuration, entry points, or component presence")
    elif (
        any(x in imports for x in ("unittest", "mock"))
        or "monkeypatch" in fixtures
        or "mocker" in fixtures
        or re.search(r"\bpatch\s*\(", text)
    ):
        label = "unit"
        evidence.append("uses mocks/patching to isolate behavior")
    else:
        label = "unit"
        evidence.append(
            "tests local behavior without a stronger static integration/contract signal"
        )

    if "unittest.mock" in text or "monkeypatch" in fixtures or "mocker" in fixtures:
        evidence.append("also uses mocking/patching")
    if "subprocess" in imports and label != "e2e":
        evidence.append("contains subprocess use, but no stronger end-to-end signal")
    return {
        "file": path.relative_to(ROOT.parent).as_posix(),
        "class": label,
        "rationale": "; ".join(evidence),
    }


def main() -> int:
    files = sorted(ROOT.rglob("test_*.py"))
    records = [classify(path) for path in files]
    if any(record["class"] not in CLASSES for record in records):
        raise SystemExit("internal error: invalid classification")
    OUTPUT.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    counts = {kind: sum(record["class"] == kind for record in records) for kind in sorted(CLASSES)}
    print("Test-class inventory:")
    for kind, count in counts.items():
        print(f"  {kind}: {count}")
    print(f"  total test files: {len(records)}")
    print(f"Wrote {OUTPUT.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
