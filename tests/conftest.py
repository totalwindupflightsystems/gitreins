"""
Shared pytest fixtures for GitReins tests.
axiom:trace work_item=GR-001 spec=specs/05-Task-Manager.md plan=.memory-bank/work-items/GR-001/plan.yaml step=step-1-1-1-1
"""

import os
import tempfile
import shutil
from pathlib import Path
import pytest


@pytest.fixture
def tmp_workdir(tmp_path):
    """Create a temporary git repository with .gitreins/ directory.

    Returns a clean workdir path that acts as a realistic GitReins workspace.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir()
    git_dir = workdir / ".git"
    git_dir.mkdir()
    # Create minimal git config so git commands work
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n")
    (git_dir / "objects").mkdir()
    (git_dir / "refs").mkdir()
    (git_dir / "refs" / "heads").mkdir()
    return str(workdir)


@pytest.fixture
def task_manager(tmp_workdir):
    """Create a TaskManager with a clean temp directory."""
    from engine.task_manager import TaskManager
    tm = TaskManager(tmp_workdir)
    return tm


@pytest.fixture
def sample_task_dict():
    """Return a sample task dict for testing."""
    return {
        "id": "test-task-1",
        "title": "Implement login endpoint",
        "criteria": [
            "Accepts email+password",
            "Returns JWT on success",
            "Returns 401 on failure",
        ],
    }


@pytest.fixture
def guard_manager(tmp_workdir):
    """Create a GuardManager with a clean temp directory."""
    from engine.guard_manager import GuardManager
    return GuardManager(tmp_workdir)


@pytest.fixture
def llm_client():
    """Create an LLMClient with default (non-functioning) settings.

    Tests that use this must mock requests.post to avoid real HTTP calls.
    """
    from engine.llm import LLMClient
    return LLMClient(base_url="https://test.local/v1", api_key="test-key-12345")


@pytest.fixture
def evaluator(llm_client, tmp_workdir):
    """Create an AgenticEvaluator with a real workdir and mockable LLM."""
    from engine.evaluator import AgenticEvaluator
    return AgenticEvaluator(llm_client, tmp_workdir, max_iterations=5)


@pytest.fixture
def judge(llm_client, tmp_workdir):
    """Create a Judge with a clean temp directory."""
    from engine.judge import Judge
    return Judge(llm_client, tmp_workdir)


@pytest.fixture
def pipeline_config_default():
    """Return a default pipeline configuration dict."""
    return {
        "pipeline": {
            "stages": [
                {
                    "id": "tier1",
                    "parallel": True,
                    "on": ["pre-commit", "pre-eval"],
                    "steps": [
                        {"id": "secrets", "type": "script", "run": "echo ok", "on_fail": "continue"},
                        {"id": "lint", "type": "script", "run": "echo ok"},
                        {"id": "tests", "type": "script", "run": "echo ok"},
                    ],
                },
                {
                    "id": "tier2",
                    "type": "ai_eval",
                    "on": ["pre-eval"],
                    "condition": "true",
                    "max_iterations": 20,
                },
            ]
        }
    }
