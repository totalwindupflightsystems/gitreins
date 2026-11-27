# GitReins dogfood evidence — 2026-11-27

Tick: gitreins-dogfood-2026-11-27-20-20-21
Target revision: b82431316425836eea360f386d6d94d19659a749
Canonical remote: github/main, parity 0/0 at capture

Real-use consumer: a fresh disposable Git repository ran the documented source-checkout CLI workflow with /home/kara/gitreins/.venv/bin/gitreins 0.22.0. install, init, doctor --json, guard --full, fingerprint --json, task create/start/complete --skip-tier2, task list, report, and a guarded commit all completed. The task verdict was explicitly Tier-1-only, not a Tier-2 judged pass.

Timing evidence:
- fingerprint --json warm: 0.31s, 0.34s, 0.40s; rc=0 for every arm.
- fingerprint --json cold-import arm: 0.29s, isolated HOME/XDG cache; rc=0. This is cold import/config state, not a cold OS page-cache claim.
- guarded docs commit: 1.06s; DEGRADED PASS because lint and static-analysis had no staged matching files; secrets and full tests ran.
- No user-noticeable performance finding; no PERF row filed.

Fresh-machine install leg:
- bunker-qa.sh launch/collect on bunker-las-03, agent 36a8d5cd, evidence rows 29, evidence SHA256 f932d057180af3306f65de87bdee7550b70f0b67b46b9967e9f29d0fd109c738.
- fresh-install OK for gitreins 0.22.0; lint OK; native tests OK; upgrade 0.21.0 -> HEAD OK; chaos-disconnect OK.
- ci-pass/ci-act FAIL/UNVERIFIED in the act simulation; the retained detail is truncated by the battery and no product cause is asserted. Docker lifecycle was SKIPPED by the live-controller safety gate; UI and corruption cells were N/A.
- Agent was retained by --keep and collect completed; teardown remains intentionally open for a later owner/TTL cleanup because this tick did not issue a destroy.

Board evidence: one recurrence finding was minted for the current onboarding install documentation drift. Existing matching row: DF-GITREINS-POC-63-R2. New row id: DF-GITREINS-POC-63-R2-R2. No other finding was filed: stale resolution-index output is already owned by GR-226, and act output is unverified rather than a product defect.

Raw controller captures are summarized here; the battery's durable evidence was copied from ~/.hermes/qa/evidence/gitreins/qa-20261127T205437Z and the controller evidence path before closeout.
