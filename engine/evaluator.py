"""
Agentic Evaluator — Iterative LLM loop that judges code completeness.

The evaluator receives task criteria and iterates: read files, run tests,
search patterns — until it has enough evidence to deliver a verdict.

7 tools available to the LLM:
  1. read_file(path)         — Read any file in the working tree
  2. run_command(cmd)        — Run a shell command (tests, lint, build)
  3. search_pattern(regex)   — Grep the codebase for a pattern
  4. read_diff()             — Show staged changes
  5. get_task_item(id)       — Read a task's criteria
  6. sandbox_write(key, content) / sandbox_read(key) — Scratch space

Usage:
    evaluator = AgenticEvaluator(llm_client, workdir="/path/to/repo")
    verdict = evaluator.evaluate(task)
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

from engine.llm import LLMClient, ToolCall
from engine.eval_cap import EvalCap, parse_eval_cap, eval_cap_from_config

logger = logging.getLogger("gitreins.evaluator")

EVALUATOR_SYSTEM_PROMPT = """You are a code quality evaluator. Your job is to judge whether a completed task meets ALL of its defined criteria.

## YOUR TOOLS

You have tools to read files, run commands, and search the codebase. Use them aggressively — don't guess. Read the actual code, run the actual tests.

If available, use read_static_analysis to check for type errors and static analysis warnings before judging criteria — type violations are hard evidence for FAIL.

For each criterion:
1. Read the relevant files
2. Run any related tests
3. Verify the code actually does what the criterion demands
4. Record your finding with specific evidence (file paths, line numbers, test output)

## EFFICIENCY RULES

- **Do not re-read the same file twice.** If read_file returned content, you already have it.
- **Do not re-run the same command.** Check previous results before running again.
- **Do not search for the same pattern twice.** Use sandbox to track what you've checked.

## VERDICT FORMAT

When you've checked EVERY criterion, deliver your verdict. You MUST output EXACTLY this JSON format with NO surrounding text, NO markdown fences, NO commentary:

{"verdict":"COMPLETE","items":[{"criterion":"<exact criterion text>","status":"PASS","detail":"<specific file:line evidence>"},{"criterion":"<exact criterion text>","status":"FAIL","detail":"<what is missing and where>"}],"summary":"<one sentence>"}

CRITICAL RULES:
- Output ONLY the JSON object. Nothing before. Nothing after.
- "verdict" must be "COMPLETE" or "INCOMPLETE" — nothing else
- "status" must be "PASS" or "FAIL" — nothing else
- EVERY criterion from the task must have a corresponding item
- PASS requires concrete evidence (file path, line number, or test output)
- FAIL requires explaining exactly what's missing
- Read actual code — never assume"""

EVALUATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the working tree. For large files, use offset/limit to read specific line ranges. First call without offset/limit to get the file size.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root."},
                    "offset": {"type": "integer", "description": "Line number to start from (1-indexed). Omit to read from beginning."},
                    "limit": {"type": "integer", "description": "Max lines to return. Omit for full file."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Returns exit code, stdout, and stderr. Use for tests, lint, build. Do NOT re-run the same command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to run."}
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_pattern",
            "description": "Search the codebase for a regex pattern. Returns matching files and lines. Do NOT repeat the same regex search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "regex": {"type": "string", "description": "Python regex pattern."},
                    "file_glob": {"type": "string", "description": "Optional: glob to filter files (e.g., '*.py')."},
                },
                "required": ["regex"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_static_analysis",
            "description": "Get static analysis diagnostics (type errors, warnings) for the project. Returns structured diagnostics from tools like mypy and pyright. Use this to check for type safety before judging criteria.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional: specific file or directory path. Omit to get diagnostics for all files."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_diff",
            "description": "Show git diff of staged and unstaged changes. Summarizes what code was changed.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_item",
            "description": "Get the full definition of a task including ALL criteria. Always call this first to know what to check.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task ID to fetch."}
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_write",
            "description": "Write to the evaluator's scratch space. Use to track which criteria you've checked, files you've read, and commands you've run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to store under (e.g., 'checked', 'evidence')."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_read",
            "description": "Read from the evaluator's scratch space to check what you've already verified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to read."}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_dead_code",
            "description": "Detect dead code: unreachable code, unused functions, empty functions, and unused imports. Returns per-file findings with line numbers.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skylos_scan",
            "description": "Multi-language dead code and AI-mistake scan via Skylos. Detects unused functions, imports, classes, variables, unreachable code, and AI-hallucinated patterns across Python, TS/JS, Go, Java, PHP, Rust, Dart, C#. Returns grade and per-file findings.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class VerdictItem:
    criterion: str
    status: str  # "PASS" | "FAIL"
    detail: str


@dataclass
class Verdict:
    verdict: str  # "COMPLETE" | "INCOMPLETE"
    items: list[VerdictItem] = field(default_factory=list)
    summary: str = ""


class AgenticEvaluator:
    """The evaluator loop: LLM iterates with tools until it delivers a verdict.

    Caps can be set via:
      - eval_cap string: "100", "30m", "200k/50k", "100/30m/200k/50k", "-1" (unlimited)
      - eval_cap EvalCap object: pre-parsed cap
      - max_iterations int: simple iteration cap (backward compat)
      - .gitreins/config.yaml: evaluator.cap key (auto-loaded if no cap specified)

    Priority: eval_cap param > max_iterations param > config.yaml > default (100 iter)
    """

    def __init__(
        self,
        llm: LLMClient,
        workdir: str = ".",
        max_iterations: int | None = None,
        eval_cap: str | EvalCap | None = None,
        command_timeout: int = 30,
    ):
        self.llm = llm
        self.workdir = os.path.abspath(workdir)

        # Resolve eval cap — explicit param wins, then max_iterations, then config
        if isinstance(eval_cap, EvalCap):
            self.eval_cap = eval_cap
        elif isinstance(eval_cap, str):
            self.eval_cap = parse_eval_cap(eval_cap)
        elif max_iterations is not None and max_iterations > 0:
            # Explicit positive value — use it directly
            self.eval_cap = EvalCap(max_iterations=max_iterations, source=f"max_iterations={max_iterations}")
        else:
            # max_iterations=None or max_iterations<=0 — read from config.yaml
            config = self._load_config()
            self.eval_cap = eval_cap_from_config(config)

        # max_iterations from EvalCap is authoritative — 100 by default, -1 for unlimited
        self.max_iterations = self.eval_cap.max_iterations if self.eval_cap.max_iterations > 0 else 10_000

        # Command timeout for tool calls — configurable for tests
        self.command_timeout = command_timeout

        self._sandbox: dict[str, str] = {}
        self._task_index: dict[str, dict] = {}
        self._files_read: set[str] = set()
        self._commands_run: set[str] = set()
        self._searches_done: set[str] = set()

    def _load_config(self) -> dict:
        """Load .gitreins/config.yaml if present."""
        import yaml
        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.isfile(config_path):
            try:
                with open(config_path) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def evaluate(self, task: dict) -> Verdict:
        """Run the agentic loop against a task and return a verdict.

        Args:
            task: dict with 'id', 'title', 'criteria' (list of strings)

        Returns:
            Verdict with pass/fail for each criterion.
        """
        # Reset state
        self._sandbox.clear()
        self._files_read.clear()
        self._commands_run.clear()
        self._searches_done.clear()

        # Build the task prompt
        criteria_list = task.get("criteria", [])
        criteria_text = "\n".join(
            f"  {i+1}. {c}" for i, c in enumerate(criteria_list)
        )
        task_prompt = f"""Evaluate this completed task:

TASK: {task.get('title', task.get('id', 'unnamed'))}
ID: {task.get('id', 'unknown')}

CRITERIA TO VERIFY (all {len(criteria_list)} must be checked):
{criteria_text}

Start by calling get_task_item("{task.get('id', 'unknown')}") to see the full task definition.
Then read the relevant code, run tests, and search for patterns.
Use sandbox_write to track which criteria you have verified.
Output ONLY the JSON verdict when done — no markdown fences, no extra text."""

        self._task_index[task.get("id", "unknown")] = task

        messages: list[dict] = [
            {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ]

        # Build tools list based on config
        config = self._load_config()
        evaluator_cfg = config.get("evaluator", {})
        tools = list(EVALUATOR_TOOLS)  # copy
        if not evaluator_cfg.get("static_analysis_diagnostics", False):
            tools = [t for t in tools if t["function"]["name"] != "read_static_analysis"]

        # Start tracking
        self.eval_cap.start()

        # Determine iteration cap. If unlimited, use a safety max.
        iter_limit = self.eval_cap.max_iterations_int

        for iteration in range(iter_limit):
            # Check caps (handles time/token limits even with unlimited iterations)
            cap_error = self.eval_cap.check()
            if cap_error:
                logger.warning("Eval cap exceeded: %s", cap_error)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Cap exceeded: {cap_error}",
                )

            try:
                response = self.llm.chat(messages, tools=tools)
            except Exception as e:
                logger.error("LLM call failed on iteration %d: %s", iteration, e)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Evaluator error: LLM call failed: {e}",
                )

            # Track LLM call (costs 1.0 iterations + token usage)
            prompt_tok = response.usage.prompt_tokens if response.usage else 0
            completion_tok = response.usage.completion_tokens if response.usage else 0
            cache_read = response.usage.cache_read_tokens if response.usage else 0
            cache_write = response.usage.cache_write_tokens if response.usage else 0
            cap_error = self.eval_cap.record_llm_call(
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            if cap_error:
                logger.warning("Eval cap exceeded: %s", cap_error)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Cap exceeded: {cap_error}",
                )

            # No tool calls → LLM is delivering a verdict
            if not response.tool_calls:
                if response.content:
                    return self._parse_verdict(response.content)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary="Evaluator returned empty response.",
                )

            # Add assistant message with tool calls
            assistant_msg: dict = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            messages.append(assistant_msg)

            # Execute each tool call — with dedup hints
            for tc in response.tool_calls:
                result, was_dup = self._execute_tool_with_dedup(tc)

                # Track tool call (costs tool_call_weight, default 0.1 iterations)
                cap_error = self.eval_cap.record_tool_call()
                if cap_error:
                    logger.warning("Eval cap exceeded during tool call: %s", cap_error)
                    return Verdict(
                        verdict="INCOMPLETE",
                        summary=f"Cap exceeded: {cap_error}",
                    )

                # Add dedup warning to result if this was a repeat
                if was_dup:
                    if isinstance(result, dict):
                        result["_dedup_warning"] = (
                            f"You already used {tc.name} with these arguments. "
                            "See previous result above. Move on to unchecked criteria."
                        )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })

            logger.debug("Evaluator iteration %d: %d tool calls, %d messages",
                         iteration + 1, len(response.tool_calls), len(messages))

        # Hit iteration cap — return clear error
        msg = (
            f"Cap exceeded: {self.eval_cap.summary()}. "
            "Increase caps (eval_cap param or evaluator.cap in .gitreins/config.yaml) "
            "or split criteria into focused single-criterion tasks."
        )
        logger.warning(msg)
        return Verdict(verdict="INCOMPLETE", summary=msg)

    def _execute_tool_with_dedup(self, tc: ToolCall) -> tuple[dict, bool]:
        """Execute a tool and return (result, was_duplicate)."""
        was_dup = False

        if tc.name == "read_file":
            path = tc.arguments.get("path", "")
            if path in self._files_read:
                was_dup = True
            else:
                self._files_read.add(path)
        elif tc.name == "run_command":
            cmd = tc.arguments.get("cmd", "")
            if cmd in self._commands_run:
                was_dup = True
            else:
                self._commands_run.add(cmd)
        elif tc.name == "search_pattern":
            regex = tc.arguments.get("regex", "")
            if regex in self._searches_done:
                was_dup = True
            else:
                self._searches_done.add(regex)

        result = self._execute_tool(tc)
        return result, was_dup

    def _execute_tool(self, tc: ToolCall) -> dict:
        """Execute a tool call and return the result."""
        try:
            if tc.name == "read_file":
                return self._tool_read_file(**tc.arguments)
            elif tc.name == "run_command":
                return self._tool_run_command(**tc.arguments)
            elif tc.name == "search_pattern":
                return self._tool_search_pattern(**tc.arguments)
            elif tc.name == "read_diff":
                return self._tool_read_diff()
            elif tc.name == "get_task_item":
                return self._tool_get_task_item(**tc.arguments)
            elif tc.name == "sandbox_write":
                return self._tool_sandbox_write(**tc.arguments)
            elif tc.name == "sandbox_read":
                return self._tool_sandbox_read(**tc.arguments)
            elif tc.name == "detect_dead_code":
                return self._tool_detect_dead_code()
            elif tc.name == "skylos_scan":
                return self._tool_skylos_scan()
            elif tc.name == "read_static_analysis":
                return self._tool_read_static_analysis(**tc.arguments)
            else:
                return {"error": f"Unknown tool: {tc.name}"}
        except Exception as e:
            logger.exception("Tool %s failed", tc.name)
            return {"error": str(e)}

    # ── Tool implementations ──────────────────────────────────────

    def _tool_read_file(self, path: str, offset: int = 0, limit: int = 0) -> dict:
        """Read a file from the working tree with optional line-range support.

        Args:
            path: File path relative to workdir.
            offset: Start line (1-indexed). 0 = from beginning.
            limit: Max lines to return. 0 = no limit.
        """
        full_path = os.path.join(self.workdir, path)
        real = os.path.realpath(full_path)
        if not real.startswith(os.path.realpath(self.workdir)):
            return {"error": f"Path outside working tree: {path}"}
        if not os.path.exists(real):
            return {"error": f"File not found: {path}"}
        if os.path.isdir(real):
            return {"error": f"Path is a directory: {path}"}
        try:
            with open(real, "r", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            total_chars = sum(len(line) for line in lines)

            # Apply offset/limit
            if offset > 0:
                start_idx = offset - 1
                if start_idx >= total_lines:
                    return {"error": f"Offset {offset} exceeds file length ({total_lines} lines)", "path": path, "total_lines": total_lines}
                lines = lines[start_idx:]
            if limit > 0:
                lines = lines[:limit]

            content = "".join(lines)

            # Only truncate if no range was requested AND file is very large
            if not offset and not limit and total_chars > 12000:
                content = "".join(lines[:400])  # First 400 lines
                content += f"\n\n... [showing first 400 of {total_lines} lines, {total_chars} chars. Use offset/limit to read specific ranges.]"

            return {
                "path": path,
                "content": content,
                "total_lines": total_lines,
                "total_chars": total_chars,
                "shown_lines": len(lines),
                "has_more": (offset > 0 and (offset - 1 + limit < total_lines)) or (not offset and not limit and total_chars > 12000) or (offset > 0 and not limit),
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_run_command(self, cmd: str = None, command: str = None) -> dict:
        """Run a shell command."""
        cmd = cmd or command
        if not cmd:
            return {"error": "No command provided"}
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                cwd=self.workdir,
            )
            output = result.stdout + result.stderr
            if len(output) > 4000:
                output = output[:4000] + f"\n... [truncated, exit_code={result.returncode}]"
            return {
                "cmd": cmd,
                "exit_code": result.returncode,
                "output": output,
            }
        except subprocess.TimeoutExpired:
            return {"cmd": cmd, "error": f"Command timed out after {self.command_timeout}s"}
        except Exception as e:
            return {"cmd": cmd, "error": str(e)}

    def _tool_search_pattern(self, regex: str, file_glob: str = "*") -> dict:
        """Search the codebase for a regex pattern."""
        matches: list[str] = []
        try:
            pattern = re.compile(regex)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        skip_dirs = {".git", "venv", ".venv", "node_modules", "__pycache__", ".gitreins-sandbox", ".pytest_cache"}
        files_searched = 0
        for root, dirs, files in os.walk(self.workdir):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for fname in files:
                if file_glob != "*":
                    import fnmatch
                    if not fnmatch.fnmatch(fname, file_glob):
                        continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, self.workdir)
                files_searched += 1
                try:
                    if os.path.getsize(fpath) > 500_000:
                        continue  # Skip large files
                    with open(fpath, "r", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if pattern.search(line):
                                matches.append(f"{rel}:{i}: {line.rstrip()}")
                except Exception:
                    pass

            if len(matches) > 200:
                matches = matches[:200]
                matches.append(f"... [truncated at 200 matches, searched {files_searched} files]")
                break

        return {"regex": regex, "matches": matches, "count": len(matches)}

    def _tool_read_diff(self) -> dict:
        """Show git diff."""
        try:
            staged = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                capture_output=True, text=True, timeout=10, cwd=self.workdir,
            )
            unstaged = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True, text=True, timeout=10, cwd=self.workdir,
            )
            return {
                "staged": staged.stdout.strip() or "(no staged changes)",
                "unstaged": unstaged.stdout.strip() or "(no unstaged changes)",
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_read_static_analysis(self, path: str | None = None) -> dict:
        """Return static analysis diagnostics for the project or a specific path."""
        from engine.static_analysis import run_static_check

        config = self._load_config()
        evaluator_cfg = config.get("evaluator", {})
        if not evaluator_cfg.get("static_analysis_diagnostics", False):
            return {"error": "static_analysis_diagnostics is not enabled in .gitreins/config.yaml"}

        guards_cfg = config.get("guards", {})
        static_tools = guards_cfg.get("static_analysis_tools", {})

        # Determine which language tools to run
        lang_tools: list[str] = []
        for lang, tools in static_tools.items():
            lang_tools.extend(tools)

        if not lang_tools:
            return {"diagnostics": [], "note": "No static analysis tools configured"}

        # Determine workdir: if path is provided, use its parent; otherwise project root
        import os
        target_dir = self.workdir
        if path:
            abs_path = os.path.join(self.workdir, path)
            if os.path.isfile(abs_path):
                target_dir = os.path.dirname(abs_path)
            elif os.path.isdir(abs_path):
                target_dir = abs_path

        all_diagnostics: list[dict] = []
        for tool in lang_tools:
            try:
                diags = run_static_check(tool, target_dir)
                all_diagnostics.extend(diags)
            except Exception as exc:
                all_diagnostics.append({
                    "tool": tool, "error": str(exc),
                    "file": "", "line": 0, "severity": "error",
                    "message": f"Tool failed: {exc}", "code": "",
                })

        return {
            "diagnostics": all_diagnostics,
            "count": len(all_diagnostics),
            "tools_used": lang_tools,
        }

    def _tool_get_task_item(self, id: str) -> dict:
        """Get task definition."""
        task = self._task_index.get(id)
        if task:
            return dict(task)
        return {"error": f"Task not found: {id}"}

    def _tool_sandbox_write(self, key: str, content: str) -> dict:
        """Write to sandbox."""
        self._sandbox[key] = content
        return {"key": key, "written": len(content)}

    def _tool_sandbox_read(self, key: str) -> dict:
        """Read from sandbox."""
        if key in self._sandbox:
            content = self._sandbox[key]
            if len(content) > 4000:
                content = content[:4000] + "... [truncated]"
            return {"key": key, "content": content}
        return {"error": f"Key not found: {key}"}

    def _parse_verdict(self, content: str) -> Verdict:
        """Parse JSON verdict from LLM response — robust against markdown fences and extra text."""
        logger.debug("Parsing verdict from: %.200s", content)

        # Strategy 1: Strip markdown fences
        cleaned = content.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            # Remove closing fence
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)

        # Strategy 2: Find JSON object boundaries
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            json_str = cleaned[start:end + 1]
            try:
                data = json.loads(json_str)

                # Validate required fields
                if "verdict" not in data or "items" not in data:
                    raise ValueError("Missing required fields 'verdict' or 'items'")

                verdict_val = str(data["verdict"]).upper()
                if verdict_val not in ("COMPLETE", "INCOMPLETE"):
                    verdict_val = "INCOMPLETE"

                items = []
                for item in data.get("items", []):
                    status = str(item.get("status", "FAIL")).upper()
                    if status not in ("PASS", "FAIL"):
                        status = "FAIL"
                    items.append(VerdictItem(
                        criterion=item.get("criterion", "unknown"),
                        status=status,
                        detail=item.get("detail", ""),
                    ))
                return Verdict(
                    verdict=verdict_val,
                    items=items,
                    summary=data.get("summary", ""),
                )
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("JSON parse failed: %s", e)

        # Strategy 3: Keyword-based fallback
        content_lower = content.lower()
        if '"complete"' in content_lower or 'verdict":"complete"' in content_lower.replace(" ", ""):
            verdict = "COMPLETE"
        elif "all criteria" in content_lower and "pass" in content_lower:
            verdict = "COMPLETE"
        else:
            verdict = "INCOMPLETE"

        logger.warning("Falling back to keyword parse: verdict=%s", verdict)
        return Verdict(
            verdict=verdict,
            summary=f"(auto-parsed from non-JSON response) {content[:300]}",
        )

    def _tool_detect_dead_code(self) -> dict:
        """Run dead code detection and return structured results."""
        try:
            from engine.dead_code import DeadCodeDetector

            detector = DeadCodeDetector(self.workdir)
            report = detector.scan()
            unused = detector.find_unused_functions()
            report.findings.extend(unused)

            by_category: dict[str, list[dict]] = {}
            for f in report.findings:
                by_category.setdefault(f.category, []).append({
                    "file": f.file,
                    "line": f.line,
                    "message": f.message,
                })

            return {
                "total_findings": len(report.findings),
                "passed": report.passed,
                "by_category": {
                    cat: {"count": len(items), "items": items[:20]}
                    for cat, items in by_category.items()
                },
            }
        except ImportError:
            return {"error": "Dead code detector unavailable"}
        except Exception as e:
            return {"error": str(e)}

    def _tool_skylos_scan(self) -> dict:
        """Run Skylos multi-language dead code and AI-mistake scan."""
        try:
            import json as _json
            import subprocess as _sp

            result = _sp.run(
                ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
                capture_output=True, text=True, timeout=120,
                cwd=self.workdir,
            )
            if result.returncode != 0:
                return {"error": f"skylos exited {result.returncode}", "stderr": result.stderr[:500]}

            data = _json.loads(result.stdout)

            findings = {
                "unused_functions": [
                    {"file": f["file"], "line": f["line"], "name": f["name"]}
                    for f in data.get("unused_functions", [])
                ],
                "unused_imports": [
                    {"file": f["file"], "line": f["line"], "name": f["name"]}
                    for f in data.get("unused_imports", [])
                ],
                "dead_symbols": [
                    {"file": info["file"], "line": info["line"], "name": name}
                    for name, info in data.get("definitions", {}).items()
                    if info.get("dead")
                ],
            }

            grade = data.get("grade", {}).get("overall", {})
            return {
                "grade": f"{grade.get('letter', '?')} ({grade.get('score', '?')})",
                "total_findings": sum(len(v) for v in findings.values()),
                "findings": findings,
            }
        except FileNotFoundError:
            return {"error": "skylos not installed — pip install skylos"}
        except Exception as e:
            return {"error": str(e)}
