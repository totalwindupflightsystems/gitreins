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

logger = logging.getLogger("gitreins.evaluator")

EVALUATOR_SYSTEM_PROMPT = """You are a code quality evaluator. Your job is to judge whether a completed task meets ALL of its defined criteria.

## YOUR TOOLS

You have tools to read files, run commands, and search the codebase. Use them aggressively — don't guess. Read the actual code, run the actual tests.

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
    """The evaluator loop: LLM iterates with tools until it delivers a verdict."""

    def __init__(
        self,
        llm: LLMClient,
        workdir: str = ".",
        max_iterations: int = 15,
    ):
        self.llm = llm
        self.workdir = os.path.abspath(workdir)
        self.max_iterations = max_iterations
        self._sandbox: dict[str, str] = {}
        self._task_index: dict[str, dict] = {}
        self._files_read: set[str] = set()
        self._commands_run: set[str] = set()
        self._searches_done: set[str] = set()

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

        for iteration in range(self.max_iterations):
            try:
                response = self.llm.chat(messages, tools=EVALUATOR_TOOLS)
            except Exception as e:
                logger.error("LLM call failed on iteration %d: %s", iteration, e)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Evaluator error: LLM call failed: {e}",
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

        # Hit max iterations — force verdict
        logger.warning("Evaluator hit max iterations (%d)", self.max_iterations)
        messages.append({
            "role": "user",
            "content": "You've reached the maximum number of tool calls. Deliver your final verdict NOW. Output ONLY the JSON — no markdown, no explanation before it.",
        })
        try:
            final = self.llm.chat(messages, tools=EVALUATOR_TOOLS)
        except Exception as e:
            return Verdict(verdict="INCOMPLETE", summary=f"Max iterations, final call failed: {e}")

        if final.content:
            return self._parse_verdict(final.content)
        return Verdict(verdict="INCOMPLETE", summary="Max iterations reached, no verdict.")

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

    def _tool_run_command(self, cmd: str) -> dict:
        """Run a shell command."""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
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
            return {"cmd": cmd, "error": "Command timed out after 30s"}
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
