# GitReins dogfood integration — 2026-10-29

Tick: `gitreins-dogfood-2026-10-29-12-57-48`
Tested HEAD: `d8f0b159ad08046186069a21bed24976213ec5ea`
Verdict: OK — local CLI workflow passed and the mandatory fresh-install/native smoke leg passed; local `act` remains unverified.

## Promise

A new user can install GitReins, initialize a repository, run the guard and fingerprint, complete a Tier-1-only task, and inspect its report. A fresh bunker run must independently prove install, smoke, and teardown at the tested HEAD.

## Real-use evidence

A scratch consumer at `/tmp/gitreins-dogfood-20261029-125748/consumer` was initialized as a real Git repository. The product was installed with `uv pip install -e '.[dev]'`, reporting `gitreins 0.17.1`. The following user workflow returned exit code 0: `gitreins install`, `gitreins init`, `gitreins guard --full`, `gitreins fingerprint --json`, `gitreins task create`, `gitreins task start`, `gitreins task complete --skip-tier2`, `gitreins task list`, and `gitreins report -n 5`.

The guard reported DEGRADED PASS with explicit skips because the scratch consumer had no source files and no tests. The Tier-1-only judge likewise reported PASS with a DEGRADED note, not a full-quality claim. `fingerprint --json` returned an authoritative digest and `dirty_count: 3`. The report listed one saved verdict.

## Performance

Single-process cold timings from the same scratch consumer: editable install 0.16s; init 0.17s; `guard --full` 1.10s; `fingerprint --json` 0.21s. These operations did not approach a user-noticeable wait, so no PERF row or profile was justified. Raw evidence is in `/tmp/gitreins-dogfood-20261029-125748.out`.

## Fresh install leg

The prescribed helper was run directly: `bash ~/.hermes/scripts/bunker-qa.sh launch /home/kara/gitreins --server bunker-mvp --ttl 2h --evidence /tmp/gitreins-dogfood-20261029-125748-bunker.jsonl`. It spawned run-owned agent `a02aef60` at the tested HEAD. A subsequent `collect --evidence` returned `RUNNING`; the evidence file still contains only the `repo-head` and `launch` rows. The agent remained alive and was not destroyed while evidence was open. `bunker info` reported `docker degraded`; the server census showed four agents, including this run's 3.5 GB agent.

Install, smoke, teardown, and the final battery verdict remain unverified. This is not a silent install pass and not an install skip: the run reached launch and remains open for the owning follow-up to collect and grade. Evidence paths: `/tmp/gitreins-dogfood-20261029-125748-bunker.jsonl` and `/tmp/gitreins-dogfood-20261029-125748-collect.out`.

## Findings

No new product finding was filed. Existing `REVIEW-GITREINS-033` remains open for the serve list's zero-criteria rendering shape. No source, board, visibility, permission, or credential changes were made.

## Final bunker evidence

The battery later completed and was collected as 22 agent evidence rows plus the collect row. Fresh install and upgrade were OK at the tested HEAD; the native suite was PASS. The local `act` leg was UNVERIFIED because Docker could not copy the setup-python action fixture (`lstat .../.prettierrc.js: no such file or directory`), which is harness/runner evidence rather than a hosted-CI product verdict. The disconnect chaos cell was OK under the network cut; resource-cap was PASS; missing-config error path was OK. Docker/UI/database legs were N/A because this CLI project has no compose file, web UI, or repo state database. The run-owned agent was destroyed and the final bunker list contained three agents without `a02aef60`.

Final install verdict: PASS for the documented package install and native smoke, with local-act unverified and non-applicable cells explicitly separated. The first bounded collection was incomplete, but the final evidence closes installability and teardown.
