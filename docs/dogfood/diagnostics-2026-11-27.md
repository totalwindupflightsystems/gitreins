# Diagnostic trail — GitReins dogfood 2026-11-27

The consumer workflow was run in a disposable repository, not inside the GitReins source checkout. The source-checkout console script was `/home/kara/gitreins/.venv/bin/gitreins`; it reported version 0.22.0.

The correct sequence was `install`, `init`, `doctor --json`, `guard --full`, `fingerprint --json`, `task create`, `task start`, `task complete --skip-tier2`, `task list`, and `report`. `task complete` returned a PASS but explicitly classified it as Tier-1-only; this is not Tier-2 evidence.

The install output is the source of the onboarding finding: it created both `.git/hooks/pre-commit` and `.git/hooks/pre-push`, and printed seventeen `.gitignore` additions. The committed onboarding text still says only pre-commit and exactly eleven entries. The existing recurrence DF-GITREINS-POC-63-R2 owns this mechanism; the new row is its recurrence, not a sibling defect.

The performance capture used `/usr/bin/time` around the successful fingerprint command. Warm wall times were 0.31, 0.34, and 0.40 seconds. The isolated-HOME/XDG arm was 0.29 seconds and exited 0. No profile was justified because the operation is subsecond and no user discomfort was observed.

The fresh bunker battery completed on bunker-las-03 agent 36a8d5cd. The evidence ledger has 29 rows and SHA256 f932d057180af3306f65de87bdee7550b70f0b67b46b9967e9f29d0fd109c738. Fresh install, native tests, lint, upgrade, and disconnect passed. The act simulation failed, but the retained detail is truncated at the package-install phase; there is not enough evidence to attribute the failure to the product. Docker lifecycle was disabled because the controller is the live Hermes host. The agent remains retained by `--keep`.
