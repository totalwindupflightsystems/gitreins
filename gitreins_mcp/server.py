"""
MCP Server — stdio transport exposing task.* and commit() tools.

Primary AI coding agents (Pi, Claude, Hermes, Codex) connect via stdio
and use these tools to manage tasks and commit code through the harness.

Implements proper JSON-RPC 2.0 framing over line-delimited stdio.
Messages can span multiple lines — we buffer until we have complete JSON.
"""

import json
import logging
import os
import subprocess
import sys

from engine.task_manager import TaskManager
from engine.judge import Judge
from engine.llm import LLMClient
from engine.guard_manager import GuardManager

logger = logging.getLogger("gitreins.mcp")


class GitReinsMCPServer:
    """MCP server that primary agents connect to via stdio."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self.tasks = TaskManager(workdir)
        self.llm = LLMClient()
        self.judge = Judge(self.llm, workdir)
        self._initialized = False

        self._tools = {
            "task.create": self._task_create,
            "task.start": self._task_start,
            "task.complete": self._task_complete,
            "task.list": self._task_list,
            "task.get": self._task_get,
            "task.delete": self._task_delete,
            "commit": self._commit,
            "guard.run": self._guard_run,
            "judge.evaluate": self._judge_evaluate,
        }

    def _tool_schemas(self) -> list[dict]:
        return [
            {
                "name": "task.create",
                "description": "Create a new task with criteria that must be met before commit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique task ID (e.g., 'login-endpoint')"},
                        "title": {"type": "string", "description": "Human-readable title"},
                        "criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of completion criteria — each must be verified",
                        },
                        "workdir": {"type": "string", "description": "Absolute path to the repo. Tasks are stored in <workdir>/.gitreins/tasks.yaml. Defaults to the MCP server's workdir."},
                    },
                    "required": ["id", "title", "criteria"],
                },
            },
            {
                "name": "task.start",
                "description": "Mark a task as in-progress.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to start"},
                        "workdir": {"type": "string", "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "task.complete",
                "description": "Mark a task as complete. Triggers evaluation if LLM is configured.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to complete"},
                        "workdir": {"type": "string", "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "task.list",
                "description": "List all tasks, optionally filtered by status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "complete"],
                            "description": "Filter by status",
                        },
                        "workdir": {"type": "string", "description": "Absolute path to the repo. Defaults to the MCP server's workdir."},
                    },
                },
            },
            {
                "name": "task.get",
                "description": "Get a task by ID.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID"},
                        "workdir": {"type": "string", "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "task.delete",
                "description": "Delete a task by ID.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to delete"},
                        "workdir": {"type": "string", "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "commit",
                "description": "Create a git commit. Runs guards first. Rejects if guards fail.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Commit message"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "guard.run",
                "description": "Run Tier 1 static guards (secrets, lint, tests). Optional workdir for cross-repo use.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workdir": {"type": "string", "description": "Absolute path to the repo to guard. Defaults to the MCP server's workdir."},
                    },
                },
            },
            {
                "name": "judge.evaluate",
                "description": "Run full evaluation pipeline (Tier 1 + Tier 2) on a task.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to evaluate"},
                        "workdir": {"type": "string", "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."},
                    },
                    "required": ["id"],
                },
            },
        ]

    # ── Tool handlers ────────────────────────────────────────────

    def _task_manager_for(self, workdir: str | None = None) -> TaskManager:
        """Return TaskManager for the given workdir, or the server default."""
        if workdir:
            wd = os.path.abspath(workdir)
            if wd != self.workdir:
                return TaskManager(wd)
        return self.tasks

    def _task_create(self, id: str, title: str, criteria: list[str], workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.create(id, title, criteria)
        logger.info("Task created: %s (workdir=%s)", id, tm.workdir)
        return tm.to_dict(task)

    def _task_start(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.start(id)
        logger.info("Task started: %s (workdir=%s)", id, tm.workdir)
        return tm.to_dict(task)

    def _task_complete(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.complete(id)
        logger.info("Task completed: %s (workdir=%s)", id, tm.workdir)

        # Trigger evaluation if LLM is configured
        api_key = os.getenv("GITREINS_LLM_API_KEY", "")
        if api_key:
            try:
                j = self.judge if tm is self.tasks else Judge(self.llm, tm.workdir)
                judge_result = j.evaluate_task(task)
                return {
                    "task": tm.to_dict(task),
                    "verdict": {
                        "passed": judge_result.passed,
                        "tier1_passed": judge_result.tier1.passed if judge_result.tier1 else None,
                        "tier2_verdict": judge_result.tier2.verdict if judge_result.tier2 else None,
                        "details": [
                            {"criterion": i.criterion, "status": i.status, "detail": i.detail}
                            for i in (judge_result.tier2.items if judge_result.tier2 else [])
                        ],
                    },
                }
            except Exception as e:
                logger.exception("Evaluation failed for %s", id)
                return {
                    "task": tm.to_dict(task),
                    "verdict": {"error": str(e)},
                }

        return {"task": tm.to_dict(task), "note": "LLM not configured — skipping evaluation"}

    def _task_list(self, status: str | None = None, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        tasks = tm.list_tasks(status)
        return {"tasks": [tm.to_dict(t) for t in tasks]}

    def _task_get(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.get(id)
        if not task:
            return {"error": f"Task not found: {id}"}
        return tm.to_dict(task)

    def _task_delete(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        try:
            tm.delete(id)
            logger.info("Task deleted: %s (workdir=%s)", id, tm.workdir)
            return {"deleted": id}
        except KeyError:
            return {"error": f"Task not found: {id}"}

    def _commit(self, message: str) -> dict:
        # Check all in-progress tasks first
        in_progress = self.tasks.list_tasks("in_progress")
        if in_progress:
            return {
                "error": "Tasks still in progress — complete or delete them first",
                "tasks": [t.id for t in in_progress],
            }

        # Run guards after task check
        tier1 = self.judge.guard_manager.run_all()
        if not tier1.passed:
            return {
                "error": "Tier 1 guards failed — commit blocked",
                "details": tier1.summary,
            }

        try:
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, timeout=30,
                cwd=self.workdir,
            )
            return {
                "committed": result.returncode == 0,
                "output": result.stdout + result.stderr,
            }
        except Exception as e:
            return {"error": str(e)}

    def _guard_run(self, workdir: str = None) -> dict:
        """Run Tier 1 static guards. Accepts optional workdir for cross-repo use."""
        import yaml
        wd = os.path.abspath(workdir) if workdir else self.workdir
        # Load config from .gitreins/config.yaml (same pattern as CLI)
        config = {}
        config_path = os.path.join(wd, ".gitreins", "config.yaml")
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r") as f:
                    config = yaml.safe_load(f) or {}
            except Exception:
                pass
        gm = GuardManager(wd, config=config)
        result = gm.run_all()
        return {
            "passed": result.passed,
            "workdir": wd,
            "results": [
                {"name": r.name, "passed": r.passed, "output": r.output[:500]}
                for r in result.results
            ],
        }

    def _judge_evaluate(self, id: str, workdir: str = None) -> dict:
        """Run full evaluation pipeline. Accepts optional workdir for cross-repo use."""
        wd = os.path.abspath(workdir) if workdir else self.workdir
        if wd != self.workdir:
            tm = TaskManager(wd)
            task = tm.get(id)
            if not task:
                return {"error": f"Task not found: {id} in {wd}"}
            j = Judge(self.llm, wd)
            result = j.evaluate_task(task)
        else:
            task = self.tasks.get(id)
            if not task:
                return {"error": f"Task not found: {id}"}
            result = self.judge.evaluate_task(task)
        d = {
            "task_id": id,
            "passed": result.passed,
            "workdir": wd,
            "tier1_passed": result.tier1.passed if result.tier1 else None,
        }
        if result.tier2:
            d["verdict"] = result.tier2.verdict
            d["items"] = [
                {"criterion": i.criterion, "status": i.status, "detail": i.detail}
                for i in result.tier2.items
            ]
            d["summary"] = result.tier2.summary
        return d

    def handle_request(self, request: dict) -> dict | None:
        """Handle a single MCP JSON-RPC request."""
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        req_id = request.get("id")

        # Validate jsonrpc field per JSON-RPC 2.0 spec
        if request.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: jsonrpc field must be '2.0'"},
            }

        try:
            if method == "initialize":
                self._initialized = True
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "gitreins", "version": "0.1.0"},
                    },
                }
            elif method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": self._tool_schemas()},
                }
            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {}) or {}
                handler = self._tools.get(tool_name)
                if not handler:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    }
                result = handler(**tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
                }
            elif method == "notifications/initialized":
                return None  # No response for notifications
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                }
        except Exception as e:
            logger.exception("Error handling request: %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)},
            }

    def run_stdio(self) -> None:
        """Run the MCP server over line-delimited JSON stdio.

        Reads JSON messages — each message is terminated by a newline.
        Multi-line JSON is handled by buffering until a complete JSON
        object can be parsed (balanced braces).
        """
        buffer = ""
        for line in sys.stdin:
            buffer += line

            # Try to parse complete JSON objects from the buffer
            while buffer.strip():
                try:
                    # Try to parse the entire buffer as JSON
                    request = json.loads(buffer)
                    buffer = ""
                    response = self.handle_request(request)
                    if response is not None:
                        self._write_response(response)
                    break
                except json.JSONDecodeError:
                    # Not complete JSON yet — try to find a complete object
                    # Count braces to find the first complete JSON object
                    depth = 0
                    in_string = False
                    escape = False
                    first_brace = -1
                    split_at = -1
                    for i, ch in enumerate(buffer):
                        if escape:
                            escape = False
                            continue
                        if ch == '\\':
                            escape = True
                            continue
                        if ch == '"' and not escape:
                            in_string = not in_string
                            continue
                        if in_string:
                            continue
                        if ch in '{[':
                            if depth == 0:
                                first_brace = i
                            depth += 1
                        elif ch in '}]':
                            depth -= 1
                            if depth == 0 and first_brace >= 0:
                                split_at = i + 1
                                break

                    if split_at > 0 and first_brace >= 0:
                        json_str = buffer[first_brace:split_at]
                        buffer = buffer[:first_brace] + buffer[split_at:]
                        try:
                            request = json.loads(json_str)
                            response = self.handle_request(request)
                            if response is not None:
                                self._write_response(response)
                        except json.JSONDecodeError:
                            logger.warning("Failed to parse extracted JSON: %.100s", json_str)
                            buffer = json_str + buffer  # Put it back
                            break
                    else:
                        # Need more data
                        break

    def _write_response(self, response: dict) -> None:
        """Write a JSON-RPC response to stdout."""
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


# Standalone entry point
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    # Accept optional workdir from command line (defaults to CWD)
    workdir = sys.argv[1] if len(sys.argv) > 1 else "."
    if workdir == "stdio":
        workdir = "."  # Hermes MCP passes "stdio" — ignore it
    server = GitReinsMCPServer(workdir)
    logger.info("GitReins MCP server starting — workdir=%s", server.workdir)
    server.run_stdio()
