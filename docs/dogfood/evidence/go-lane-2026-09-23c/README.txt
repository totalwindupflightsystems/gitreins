Go guard-lane dogfood evidence — 2026-09-23c (run 9)
Target: /home/kara/gitreins HEAD beda743 ; consumer repo /tmp/dogfood-gitreins/go-app

01-full-tree-false-pass.log
  `gitreins guard --full` with internal/quota/broken.go on disk, untracked.
  console: `Tier 1 Guards: PASS  (test mode: full, whole tree)`
  log: all three Go lanes report `No Go files staged`. F1.
  Same run on the bunker box (fresh Debian, no Go toolchain) reproduced it.

02-staged-scope-noop-on-committed-head.log
  broken.go COMMITTED at HEAD, index clean, `go build ./...` fails.
  `gitreins guard` (README default) → PASS; lanes report `No Go files staged`.

03-lint-fallback-govet-clean.log
  internal/quota/lint_probe.go staged (compiles; errcheck violation on os.Mkdir).
  `gitreins guard --scope working-tree` → PASS, `✓ go_lint — ok`,
  run log output: `go vet: clean`. F2.

04-golangci-direct-errcheck.txt
  Same file, tool run directly with gitreins' own argv:
    golangci-lint run --new-from-rev=HEAD~1 internal/quota/lint_probe.go
  → `internal/quota/lint_probe.go:8:10: Error return value of os.Mkdir is not
  checked (errcheck)` / `1 issues` / exit 1 (golangci-lint 2.12.2).
  `go vet ./...` on the same tree → clean. This is the A/B behind F2.

05-staged-broken-go-FAIL.log
  internal/quota/broken.go staged (uncompilable).
  `gitreins guard` → FAIL, exit 1, lanes name
  `internal/quota/broken.go:5:9: cannot use "not an int" ... as int value`.
  Also the real `git commit` path: pre-commit hook → COMMIT_RC=1, HEAD unmoved.
  This is the lane working as promised (G2/G3).

Bunker install leg (las-bunker-03, agent 2db38df6, destroyed+verified gone):
  - README `pip3 install gitreins` → PIP_RC=1, PEP-668 externally-managed.
  - venv install → ~24s to a working `gitreins --version`.
  - clone https://github.com/totalwindupflightsystems/gitreins → OK, HEAD=beda743
    (existing public access; no visibility/permission change made).
  - `gitreins init` on a Go repo → Language: Go, Test cmd: go test -short -count=1 ./...
  - broken .go staged → `✗ go_build`/`✗ go_lint`/`✗ go_tests`, exit 1,
    commit refused; log says `error: [Errno 2] No such file or directory: 'go'`
    (no toolchain on that box) — console shows a bare ✗. F5.
  - same file unstaged → `Tier 1 Guards: PASS (test mode: full, whole tree)`. F1.

No credentials, tokens, or real user data appear in any artifact here.
The scratch repos live under /tmp/dogfood-gitreins/ and are not committed.
