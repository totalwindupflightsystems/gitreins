# Integration report — GitReins dogfood 2026-11-27

Promise: a user can install GitReins in a repository, initialize the guard configuration, run Tier-1 checks, create and complete a criteria task, inspect its verdict, and commit through the hook.

Result: the promise held on a fresh disposable consumer repository using the repository's 0.22.0 CLI. First successful command was `gitreins --version` (0.22.0); the documented workflow reached a guarded commit in 1.06 seconds after install/init. The task completion was `--skip-tier2` and the output explicitly said `tier1-only`, so no LLM-judged completion is claimed here.

Observed friction:
1. `gitreins init` says static analysis is enabled, but the first guarded docs commit correctly reported static-analysis skipped because no staged Python files matched. This is honest but requires the user to understand DEGRADED PASS semantics.
2. `gitreins doctor --json` on the consumer reported an unwired test symbol, which is expected for a tiny consumer but creates warning noise for a new user.
3. The onboarding guide still says install writes only pre-commit and exactly eleven exclusions. The live install wrote pre-commit and pre-push plus seventeen `.gitignore` additions, including quality and review state files. This is the filed recurrence DF-GITREINS-POC-63-R2-R2.

Performance: fingerprint was 0.31/0.34/0.40 seconds warm and 0.29 seconds in an isolated HOME/XDG cold-import arm. These are below a user-noticeable threshold; no PERF finding.

Fresh-machine evidence: bunker agent 36a8d5cd on bunker-las-03 installed 0.22.0 successfully and passed the native suite, lint, upgrade, and disconnect cells. The local act simulation failed and remains unverified as a workflow result; its retained detail was truncated, so no product cause is inferred. Docker lifecycle was safety-skipped on the live controller. The agent was retained by `--keep`; cleanup is an open operational follow-up, not evidence of an install failure.
