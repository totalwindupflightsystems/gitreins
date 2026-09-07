\n## Dogfood Findings (2026-09-07)\nVerdict: PROMISING-BUT-ROUGH\nPromise: {"entry_point":"Python console-script CLI usage: gitreins [-h] [--version]
                {install,init,task,guard,judge,commit,commit-audit,mcp-server,security-scan,setup-tools,report}
                ...

GitReins — Git-Native Agent Co-Harness

positional arguments:
  {install,init,task,guard,judge,commit,commit-audit,mcp-server,security-scan,setup-tools,report}
    install             Install GitReins hooks and config in the current repo
    init                Smart init — detect language, size, optimal config
    task                Task management
    guard               Run Tier 1 guards
    judge               Evaluate a task
    commit              Commit with guard checks
    commit-audit        Validate commit message against staged diff (commit-
                        msg hook)
    mcp-server          Run MCP stdio server
    security-scan       Run the Antares CVE localization scanner (opt-in)
    setup-tools         Show available static analysis tools and install
                        instructions
    report              Show verdict history

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit, with an optional MCP stdio server launched by .","promise":"Promise: this project claims a developer or AI coding agent can manage criteria-based tasks, verify code with static guards and an agentic LLM evaluator, and prevent\n\n- [P0] The documented evaluated-task-to-commit workflow does not preserve the evaluated payload — After task complete passed Tier 1 and criterion-level Tier 2 evaluation, calculator.py and test_calculator.py were silently removed from the index. The immediately following gitreins commit exited 0 b\n- [P1] A successful commit can be materially incomplete without warning — The harness reported success even though the evaluated implementation and its five passing tests were absent from the resulting commit. Users must independently run git show --name-only and git status\n- [P1] Fresh initialization is inconsistent and leaves unexplained artifacts — Init announced 'uv run pytest -x --tb=short' but persisted 'pytest -x --tb=short', enabled static_analysis despite README default-off language, and left config.yaml.bak, usage.jsonl, and __pycache__/ \n- [P1] The product has real value but high workflow friction — Installation produced a working CLI in 15 seconds, and guards, five tests, task lifecycle, Tier 2 evaluation, reports, and all 12 advertised MCP tools worked against real data; however, the run record\n- [P2] Diagnostics, versioning, and MCP onboarding weaken usability and trust — Tier 1 evidence was truncated mid-line, versions disagreed across CLI 0.12.1, README 0.12.0, and MCP 0.1.0, and the stdio server lacked startup acknowledgement or a documented JSON-RPC client example,
