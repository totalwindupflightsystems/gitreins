---
name: gitreins-release-and-versions
description: >-
  Use when you must establish which GitReins build is actually running, compare
  the installed copy against the source checkout and the released package, prove
  a merged fix is really in the deployed binary, cut or verify a release, or
  upgrade a host without silently keeping stale code. Covers the version-identity
  chain (pyproject -> importlib.metadata -> `gitreins --version` -> PyPI), the
  multi-binary discriminator commands that separate a repo venv, a pipx install
  and a PATH shadow, the `scripts/check_deployed_surface.py` probe and the exact
  meaning (and blind spots) of `aligned: true`, why a repo is routinely dozens of
  commits ahead of the published PyPI package, the tag==version release gate, the
  pre-commit hook's interpreter pinning, a post-upgrade re-verification
  checklist, and the honesty rule that a version is never quoted from memory or
  from a shell that may have cached a different binary.
version: 1.0.0
category: software-development
---

# GitReins Release and Versions

> **Reading the ids.** References like `DF-010`, `GR-GAP-055`, `POC-12` or `INT-CI-8` are rows on this project's own
> internal work board. They are kept so a claim can be traced to the incident that produced it;
> nothing in this skill requires knowing what they contain.

Version strings are not identity. GitReins ships as a Python package, so at any
moment a host can hold three or four different "0.15.0"s that execute different
code: the repo checkout, the repo's editable `.venv`, a pipx install, a
PATH-shadowing venv, and the wheel on PyPI. Every one of them answers
`gitreins --version` with the same number while behaving differently. This skill
is how you tell them apart, how you find the drift, and how you upgrade without
lying to yourself about it.

Load this skill when: you are about to claim a GitReins fix is live; a guard
behaves differently than the source says it should; you need to release; you are
upgrading a host; or you are asked "what version of GitReins is running".

Everything in the **Live state** section below was executed on this host on
**2026-09-27 ~06:36Z**. Re-run the commands — versions move, and a number copied
from this document is exactly the sort of memory-quoted version this skill exists
to forbid.

---

## 1. The version-identity chain

There is exactly one declared version in the tree, and one derivation path from
it to every surface that reports a version:

```
pyproject.toml [project].version        <- the only hand-edited version
        |
        v
engine/version.py  metadata.version("gitreins")   <- installed metadata first
        |          else pyproject.toml line-scan  <- bare source checkout
        |          else "0.0.0.dev"               <- last resort
        v
gitreins/cli.py    parser.add_argument("--version", action="version", ...)
        v
`gitreins --version`  /  MCP serverInfo.version  /  README banner
```

Citations:

- `pyproject.toml:7` — `version = "0.15.0"` (the single declared version).
- `engine/version.py:20` — `__version__ = metadata.version("gitreins")`.
- `engine/version.py:23-25` — bare-checkout fallback reads `pyproject.toml`.
- `engine/version.py:27` — last-resort placeholder `"0.0.0.dev"`.
- `gitreins/cli.py:3267` — `parser.add_argument("--version", action="version", version=f"gitreins {__version__}")`.
- `gitreins/cli.py:44` — `from engine.version import __version__`.
- `docs/cli-reference.md:496-501` — `gitreins MCP server <version>` and
  `python -m gitreins_mcp.server --version` must report the same value as
  `gitreins --version`.

**Why `importlib.metadata` and never a literal.** An earlier release shipped a
static `engine/version.py` literal; the 0.12.0 wheel then reported `0.11.0`
while its own `METADATA` said `0.12.0`. The release workflow now actively forbids
that regression — it extracts the built wheel, asserts `engine/version.py`
contains an `importlib` lookup, and fails if the literal `"0.11.0"` is still
present (`.github/workflows/release.yml:102-112`), then installs the wheel into a
fresh venv and asserts the CLI prints exactly `gitreins <tag_version>`
(`.github/workflows/release.yml:125-133`). If you are tempted to "simplify"
`engine/version.py` into a literal, that gate exists specifically to stop you.

Note the docstring at `engine/version.py:3-5` states the precedence explicitly.
Anything that reads the version any other way is a second source of truth and is
a bug.

---

### Path conventions used below

Host-specific absolute paths are written as shell variables so the snippets stay
copy-pasteable. Set them once:

```bash
REPO="$PWD"                                                      # the gitreins checkout
PIPX_VENV="$(pipx environment --value PIPX_LOCAL_VENVS)/gitreins" # the pipx install's venv
PIPX_BIN="$(pipx environment --value PIPX_BIN_DIR)"               # where pipx exposes apps (~/.local/bin)
PIPX_SITE="$(ls -d "$PIPX_VENV"/lib/python*/site-packages)"       # the installed package directory
```

The concrete values observed on this host are recorded inline in the evidence
below; substitute your own.

## 2. Live state on this host (2026-09-27, observed, not recalled)

Every row below is the output of a command that was actually run.

| Surface | Command | Output |
|---|---|---|
| Repo HEAD | `git rev-parse --short HEAD` | `abc7f7c` (branch `main`) |
| Repo distance from release | `git describe --tags` | `v0.15.0-150-gabc7f7c` |
| Declared version | `grep '^version' pyproject.toml` | `version = "0.15.0"` |
| Release tag commit | `git log -1 --format='%h %ad' --date=iso v0.15.0` | `7db6692 2026-09-22 13:55:51 -0500` |
| PATH resolution | `which -a gitreins` | `$PIPX_BIN/gitreins` (single entry) |
| What that path is | `readlink -f …` | `$PIPX_VENV/bin/gitreins` |
| pipx install | `pipx list` | `package gitreins 0.15.0, installed using Python 3.14.4` |
| CLI self-report | `$PIPX_BIN/gitreins --version` | `gitreins 0.15.0` |
| Repo `.venv` | `./.venv/bin/gitreins --version` | `gitreins 0.15.0` |
| Installed metadata | `grep '^Version:' …gitreins-*.dist-info/METADATA` | `Version: 0.15.0` |
| Released on PyPI | `curl -s https://pypi.org/pypi/gitreins/json \| grep '"version"'` | `"version":"0.15.0"` |
| Update-checker cache | `cat ~/.cache/gitreins/update-check.json` | `{"last_checked": 1790490872.8567233, "latest_version": "0.15.0"}` (= 2026-09-27T06:34:32Z) |
| Deployed-surface probe | `python3 scripts/check_deployed_surface.py` | `ALIGNED`, exit 0, 16 subcommands both sides |

**Six surfaces all report `0.15.0` — and two of them are running different
code.** That is the whole problem in one table:

```
$ md5sum "$PIPX_SITE/gitreins/cli.py" "$REPO/gitreins/cli.py"
e9dc20d5e62c3f43bfc618d67b250475  …/site-packages/gitreins/cli.py   (3687 lines, 142857 B, 2026-09-23 04:03)
5c54a6352ecca8ad3d67d9ae5a3993e6  $REPO/gitreins/cli.py       (3891 lines, 152562 B, 2026-09-26 16:23)
```

Different files, 204 lines apart, identical version string, and the
deployed-surface probe still says `ALIGNED`. Meanwhile
`md5sum …/engine/config.py …/engine/config.py` **matches** on both sides
(`5dd5146a02e525827d5d21056ba72e6d`), so the drift is not uniform — it is
per-file, which is exactly why you diff bytes and not versions.

Scale of that gap: `git rev-list --count v0.15.0..HEAD` = **150**;
`git diff --stat v0.15.0..HEAD -- gitreins engine` = **16 files changed, 3286
insertions(+), 442 deletions(-)**.

---

## 3. Which build is actually running: the discriminator commands

Never trust a bare `gitreins --version` as an answer to "which build is
running". Walk the identity of the process, not its self-report.

### 3.1 Resolve the path and what stands behind it

```bash
which -a gitreins                 # every copy on PATH, in resolution order
type -a gitreins                  # same, shell-aware (aliases/functions too)
readlink -f "$(which gitreins)"   # final target of the symlink chain
file -L "$(which gitreins)"       # real file, or a script with a shebang
head -c 200 "$(which gitreins)"   # the shebang names the INTERPRETER
```

The shebang is the payload. A console-script stub's first line tells you which
Python will import the code:

```bash
#!$PIPX_VENV/bin/python
```

Read that interpreter — that is the installation that will run. A pipx app
symlink in `~/.local/bin` pointing at a *different* venv than the shebang
expects is a classic false green.

### 3.2 Ask each candidate directly

A repo `.venv`, a pipx install, and a PATH shadow can all answer the same
number. Interrogate each one by absolute path — never by bare name:

```bash
/path/to/repo/.venv/bin/gitreins --version
/path/to/repo/.venv/bin/python  -m gitreins --version
"$(pipx environment --value PIPX_LOCAL_VENVS)/gitreins/bin/gitreins" --version
"$(pipx environment --value PIPX_LOCAL_VENVS)/gitreins/bin/python" -m gitreins --version
```

The `python -m gitreins` form is the sharpest probe: it imports whatever that
interpreter resolves on `sys.path`, so it reports the truth even when a console
script's shebang is stale or the symlink was hand-patched. `engine/version.py:20`
then reads that interpreter's own installed metadata.

### 3.3 Find the shadow before it bites

This is not hypothetical. A field probe walked the host and found the default
`PATH` answering **0.12.0** while the task `PATH` (with the project's `.venv`
ahead of it) answered **0.12.1**, while PyPI's latest was also **0.12.1** and
`~/.local/bin/gitreins` answered **0.12.0**. Same host, same second, three
answers. Reproduce the shape:

```bash
# enumerate every gitreins on every relevant PATH
for p in $(echo "$PATH" | tr ':' ' '); do
  [ -x "$p/gitreins" ] && printf '%s -> ' "$p/gitreins" && "$p/gitreins" --version
done
# and every venv you know about
find ~ -maxdepth 4 -type f -name gitreins -perm -u+x 2>/dev/null | head -20
```

Any answer that differs between two lines is drift you are about to be confused
by. Fix the PATH or fix the install — do not "note it and move on"; a guard that
resolved the wrong binary is a guard that did not run.

### 3.4 Is a pipx install from PyPI or from this checkout?

`pipx list` prints a version. It does **not** print provenance. A pipx app can be
installed from PyPI (`pipx install gitreins`) or from a local path
(`pipx install $REPO`), and they behave completely differently:
the local-path one is a frozen snapshot of your checkout, the PyPI one is the
published release. Discriminate with the recorded direct URL:

```bash
cat "$(pipx environment --value PIPX_LOCAL_VENVS)/gitreins/lib/python"*/site-packages/gitreins-*.dist-info/direct_url.json
```

On this host it reads `{"dir_info": {}, "url": "file://<absolute path to the checkout>"}` —
**a local-path install of the repo, not PyPI.** Consequences: this pipx copy
reports `0.15.0` and is 204 `cli.py` lines behind HEAD, and it is *not* evidence
that PyPI's 0.15.0 is built from this tree. A missing `direct_url.json` (or one
with no `dir_info`) means a normal registry install. `<binary> --version` cannot
tell you this; only provenance metadata can.

---

## 4. Installed vs source vs released: the three-way check

Track three numbers and, crucially, three *code* states:

| | how to read it | what it means |
|---|---|---|
| **Source** | `git describe --tags`, `grep '^version' pyproject.toml` | the code you are editing |
| **Installed** | the absolute-path probes of §3.2, plus `direct_url.json` | the code that will actually run your guards |
| **Released** | `curl -s https://pypi.org/pypi/gitreins/json` → `info.version`, or the update checker cache | the code a stranger gets |

```bash
# the three-way, read-only, no repo mutation
git -C $REPO describe --tags
grep '^version' $REPO/pyproject.toml
/absolute/path/to/running/gitreins --version
curl -s https://pypi.org/pypi/gitreins/json | grep -o '"version":"[^"]*"' | head -1
```

Then compare **code**, not strings — diff the installed package directory against
the checkout. This is the step that catches what version strings hide:

```bash
SP="$PIPX_SITE"   # e.g. …/lib/python3.14/site-packages on this host (see the conventions above)
for f in gitreins/cli.py engine/config.py engine/version.py; do
  md5sum "$SP/$f" "$REPO/$f"
done
```

Identical hashes → the deployed copy is byte-identical to those files at HEAD.
Different → you are running older (or foreign) code, whatever the version says.
`engine/config.py` matching while `gitreins/cli.py` does not (this host, today) is
the normal shape: a partial reinstall, or a checkout that moved after the install.

### The update checker is not a version oracle

`engine/config.py:538` `check_for_update()` reads PyPI's `info.version`
(`engine/config.py:604-611`, endpoint `https://pypi.org/pypi/gitreins/json`) and
compares it with `_version_greater()` (`engine/config.py:617-622`) against the
*currently running* `__version__`. It caches to
`~/.cache/gitreins/update-check.json` (`engine/config.py:24-27`) and respects
`update_check_ttl` (default 24h; `engine/config.py:101-102`, `559-579`). It is
invoked on guard/judge/commit paths (`gitreins/cli.py:316-325`, called at
`:2059`, `:2258`, `:2389`) and is deliberately non-blocking (`:325`) and
suppressed on `--json` (`:2055`, `:2242`).

Two traps:

- It compares against **the version the running process thinks it is** — so a
  stale install reports "up to date" relative to its own stale self. It can only
  ever tell you PyPI has something newer than *you claim to be*.
- It is **cached for up to 24h**. A cache saying `latest_version: 0.15.0`
  (this host, checked 2026-09-27T06:34:32Z) is evidence of PyPI's state at that
  moment, not now. For a release decision, hit PyPI directly.

---

## 5. The deployed-surface probe, and what `aligned` really means

`scripts/check_deployed_surface.py` is the machine-checkable answer to "is the
installed CLI the code that was merged?". It exists because of a review finding —
quoted at `scripts/check_deployed_surface.py:4-6` — that the fleet executes the
*installed* gitreins while nothing verified the installed build was the merged
one, and both surfaces reported `0.14.0`, so a stale install was invisible.

```bash
python3 scripts/check_deployed_surface.py           # human output, exit 1 on drift
python3 scripts/check_deployed_surface.py --json    # machine output
python3 scripts/check_deployed_surface.py --quiet   # print only on drift
```

What it does: it collects the **subcommand name set** from the repo CLI
(`repo_surface()`, `:46-59`, preferring `./.venv/bin/python -m gitreins --help`)
and from the installed CLI (`installed_surface()`, `:62-70`, `shutil.which`), plus
the repo version (regex over `pyproject.toml`, `:53-58`) and the installed version
(`installed_surface()` parses the last token of `--version`, `:68-69`). It is
stdlib-only and never mutates anything (`:13-15`).

The verdict:

```python
"aligned": not missing and not extra and not r_err and not i_err and (r_ver == i_ver)
```

(`scripts/check_deployed_surface.py:95`)

`missing` = merged in the repo, absent from the deployed build (`:82`) — the fleet
would run older code; the tool prints the remedy itself, `pipx install --force
<repo>` (`:105`). `extra` = deployed but not in this checkout (`:83`) — a foreign
build. Version mismatch prints `VERSION DRIFT: repo X vs deployed Y` (`:108-109`).

**`aligned: true` means exactly four things and no more:** same version string,
same subcommand *names*, no error from either CLI, and no set difference. It is
**not** "same code". Live proof on this host: the probe printed `ALIGNED` /
`exit 0` while `gitreins/cli.py` hashed differently between the deployed copy and
the checkout (§2). The probe compares *surface*, and 150 commits of behaviour
change can land entirely inside an existing subcommand's body with no new name.
`engine/version.py`, arithmetic in `guard`, the test-lane interpreter pin, the
evaluator process-group reap — none of those change the `--help` name set.

So: **use the probe as a smoke alarm, not a fire certificate.** It catches the
loud class (a subcommand that never reached the deployed build — the exact
`0.14.0`-reporting-both-sides failure it was written for). For the quiet class
(same surface, different bodies) you still need the `md5sum`/diff of §4. The
probe's own README note is honest about its authorship: it is new in 0.15.0,
introduced alongside this class of drift.

---

## 6. Release tagging, and why a repo is always ahead of PyPI

### How a release is cut

The workflow triggers on a version tag, not on a merge
(`.github/workflows/release.yml:3-6`). Gate order:

1. **Tag must equal the declared version.** `release.yml:22-40` strips the `v`,
   regex-scans `pyproject.toml` for `^version`, and fails the run on any
   mismatch: `tag vX (version X) does not match pyproject.toml version Y`.
2. **Idempotence.** `release.yml:41-67` queries PyPI `info.version`; if it equals
   the tag version it sets `already_published=true` and every publish step is
   skipped, so re-tagging an existing version yields a green run instead of a
   duplicate-upload failure (`:69`, `:72`, `:75`, `:136`).
3. **Build + wheel assertions.** `python -m build` (`:71-73`), then the wheel is
   unzipped and asserted: `engine/version.py` present, contains `importlib`, no
   static `"0.11.0"` literal, `METADATA` `Version:` equals the tag, and a fresh
   venv install prints exactly `gitreins <tag>` (`:74-133`).
4. **Publish** — `twine upload dist/*` with the token from
   `secrets.PYPI_API_TOKEN` (`:135-140`), referenced via env, never inlined. This
   is the on-disk equivalent of the older manual `TWINE_PASSWORD` procedure in the
   prior knowledge base; the token value is `[REDACTED]` everywhere and must stay
   that way.
5. **GitHub release** with generated notes (`:142-154`).

### Why the repo is routinely ahead

A tag freezes a snapshot; `main` keeps moving. That is normal and expected — on
this host `v0.15.0` is 150 commits and 3,286 insertions behind HEAD, and ~98
commits behind the pipx snapshot date. What is *not* normal is letting that gap
go unmeasured. Two real failure classes, both filed on the board:

- **Release lag shipped as a defect.** `DF-010 (P0)`: `0.11.0` predated the
  `DF-001` fix; 201 commits sat unreleased, there was no release pipeline in CI,
  and sdist-vs-HEAD drift went undetected for 11 days. A later instance,
  `GR-GAP-055 (P1)`: published PyPI `0.13.0` was **84 commits behind main and
  shipped four defects main already fixed** (one of them the wheel's own version
  header). `DF-014` is the same class one version later: `gitreins init` on PyPI
  `0.11.0` reported `Language: unknown` for a plain-Python repo, while repo HEAD
  detected Python correctly — a user-visible bug that was already fixed and simply
  unreleased.
- **Version bumped without a tag.** `GR-GAP-050`: PyPI carried `0.10.0`–`0.12.0`
  while the newest git tag was still `v0.9.1`. The published package had no
  corresponding commit marker, so "what is in 0.12.0?" had no answer. Fixed by
  tagging the released versions and by the tag==version gate now in
  `release.yml:22-40`, which makes the bump-without-tag state fail CI instead of
  shipping.

**Operational rule:** before you tell anyone a fix is available, check whether it
is *released* — not merely merged. `git describe` on a host whose install predates
the fix is the usual source of a false "it's deployed" claim. And per §4, check
whether the *local install* is even the released package: on this host the pipx
copy is a `file://` install of the checkout, so it is neither HEAD-clean nor
PyPI-pure.

Release notes legacy: `CHANGELOG.md` (present, 48 KB) is the human history and
must be updated in the same commit as the version bump; `README.md:14` carries a
release banner whose version is gated against `pyproject.toml` by
`scripts/check_docs_drift.py` Check A.

---

## 7. Version-drift symptoms: what it looks like in practice

Recognise the shape before you debug the wrong thing.

### 7.1 A fix present in source, absent in the installed binary, identical version

The canonical symptom. A behaviour you can read in the checkout does not happen
when the guards run. On this host the mechanical proof is one command:

```bash
md5sum "$SP/gitreins/cli.py" $REPO/gitreins/cli.py   # differ -> stale install
```

This is *not* reliably visible from `--version` (both say `0.15.0`) and *not*
reliably visible from the surface probe (`ALIGNED`). It is visible from bytes.

### 7.2 The wheel reported a different version than its own metadata

`DF-015 (P0)`: the 0.12.0 wheel shipped a static `engine/version.py` reporting
`0.11.0` while `METADATA` said `0.12.0`. The published artifact disagreed with
itself. Now gated at `release.yml:102-133`. If you ever see
`gitreins --version` disagree with the dist-info `Version:`, suspect the wheel
build path, not the install.

### 7.3 Different binaries have different coverage — and that changes verdicts

`DF-011 (P1)` and `DF-012 (P1)` are the same lesson from two angles. `DF-011`: the
pre-commit hook called a **bare** `gitreins`, so at commit time PATH could resolve
a different version than the one that ran `install` — and real secrets were
committed through that stale hook. `DF-012`: with gitleaks present, `sk-`/`ghp_`
patterns could pass, while without it the built-in scanner caught them — so
*which build runs the scan* changes what is caught. A guard whose behaviour
depends on PATH resolution is not a guard.

Both are structurally fixed: the hook is now pinned (§8), and
`gitreins/cli.py:137-199` documents the PATH-shadow premise in the generated hook
itself.

### 7.4 One host, two answers, same second

The multi-binary probe of §3.3: default PATH `0.12.0`, task PATH `0.12.1`, PyPI
`0.12.1`, `~/.local/bin` `0.12.0`. If two tools disagree about the version, the
bug is the resolution, not the version.

### 7.5 Docs and banners reporting a version the tree does not have

`GR-GAP-052`: the README banner said `v0.12.0` while `pyproject.toml` and PyPI
were already `0.12.1`, and the banner's test counts contradicted themselves on
the same line. Fixed by `scripts/check_docs_drift.py` (docstring
`:1-47`): Check A gates the README banner version against `pyproject.toml`,
Check B gates every `N tests pass` / `N tests across` / `N test files` claim
against a live `--collect-only` (never importing pytest, and failing closed when
collection is unmeasurable — "a gate must never certify a metric it did not
measure"), Check C gates evaluator tool-count claims against `EVALUATOR_TOOLS` by
AST parse. It is chained into CI at `.github/workflows/ci.yml:81-84` and into the
local guard's `test_command` per `AGENTS.md`.

---

## 8. The pre-commit hook pins its interpreter — and why that matters

The generated hook does **not** call a bare `gitreins`. It calls an absolute
invocation substituted at install time
(`gitreins/cli.py:137-155` for the template, `:189-199` for rendering, written to
`.git/hooks/pre-commit` at `:415` and `:573`).

The resolver (`gitreins/cli.py:158-186`) walks a fixed order and returns a
shell-quoted invocation:

1. `os.path.realpath(sys.argv[0])` (`:176`) — if it is a real executable named
   `gitreins` (`:177-182`), pin that absolute path. This is the binary that
   actually ran `install`, so it is immune to PATH shadowing both at install time
   *and* at commit time.
2. else `sys.executable -m gitreins` (`:184-185`) — the interpreter running the
   install imports the same installation by construction.
3. else `None` (`:186`), and `_render_pre_commit_hook()` (`:189-199`) emits a hook
   with a `WARNING: no gitreins binary or interpreter resolvable at install time —
   falling back to PATH lookup (a different version may run)` comment *and* a bare
   `gitreins guard`. A hook carrying that warning is an unpinned hook.

The docstring at `gitreins/cli.py:161-173` records the DF-011 premise verbatim: a
bare `gitreins` "resolves via PATH at commit time and can silently pick a
different version", and a stale earlier on PATH let real secrets through while the
repo's `.venv` had a different release.

**Why this matters across upgrades.** The pin is a path, not a version, and it is
frozen when the hook is written. Upgrade the install and the old hook keeps
invoking the *old* interpreter's installation — so a host can be "upgraded" while
commits are still gated by the previous build. Read the pin:

```bash
grep -n 'gitreins guard' .git/hooks/pre-commit
```

On this host that line is:

```bash
/path/to/hermes-venv/bin/python -m gitreins guard
```

— a **third** interpreter, neither the pipx venv nor the repo `.venv` (step 2 of
the resolver, not step 1). So this repo's commits are gated by whatever is
installed in that Hermes venv, and the version that venv imports is the version
that matters here — not the pipx one, and not `gitreins --version` on your PATH.
That is the whole point: **the hook's pinned interpreter, not your PATH, decides
which build guards a commit.** After any upgrade, re-read that line and re-run
`gitreins install` from the intended installation if it is not pinned where you
want it.

`GR-GAP-049` tracked the propagation gap for this: a stale hook running bare
`gitreins` after DF-011 was fixed in the generator.

---

## 9. Upgrade procedure and the post-upgrade checklist

Nothing here installs or upgrades anything by itself — this is the sequence and
the evidence to gather.

### 9.1 Before

```bash
date -u                                   # timestamp the observation
which -a gitreins                          # what will a bare call hit?
readlink -f "$(which gitreins)"            # what is behind it?
cat "$(pipx environment --value PIPX_LOCAL_VENVS)/gitreins/lib/python"*/site-packages/gitreins-*.dist-info/direct_url.json
grep -n 'gitreins guard' .git/hooks/pre-commit   # the pin you are about to invalidate
git -C $REPO describe --tags       # source state
curl -s https://pypi.org/pypi/gitreins/json | grep -o '"version":"[^"]*"' | head -1
```

Record all of it. A pre-upgrade baseline is the only way to prove the upgrade did
anything.

### 9.2 Install

```bash
# from the registry (what a user gets)
pipx install gitreins          # or: pipx upgrade gitreins
# from this checkout (a frozen snapshot of the tree, NOT the released package)
pipx install --force $REPO
```

The repo's own guard hint is `pipx install --force <repo>`
(`scripts/check_deployed_surface.py:105`) — that is a local-path install, which
diagnoses fine but does not mean you are running the release. Pick deliberately
and say which one you did. Then:

```bash
pipx ensurepath --force        # if the ~/.local/bin symlink did not refresh
```

If a symlink is stale, re-point it at
`$(pipx environment --value PIPX_BIN_DIR)/gitreins` rather than hand-editing.

### 9.3 After — the checklist

Re-run all of it; a version string is one line of the evidence:

1. **Path and provenance** — `which -a gitreins`, `readlink -f`,
   `direct_url.json`. Confirms you upgraded the install a bare call will hit, and
   whether it came from PyPI or the checkout.
2. **Version, from the absolute path** — `/abs/path/gitreins --version` and
   `/abs/path/python -m gitreins --version`. Consult `pyproject.toml:7` for what
   the source *should* be; never compare against a remembered number.
3. **Installed vs source bytes** — the `md5sum` loop of §4 over
   `gitreins/cli.py`, `engine/config.py`, `engine/version.py`. This is what
   actually proves the fix is deployed.
4. **Metadata agreement** — `grep '^Version:' …dist-info/METADATA` must equal
   `--version`'s output. Disagreement means a bad wheel, per §7.2.
5. **Deployed surface** — `python3 scripts/check_deployed_surface.py`. Expect
   `ALIGNED`, exit 0. Remember §5: a green here is necessary, not sufficient.
6. **The pre-commit hook's pin** — `grep -n 'gitreins guard'
   .git/hooks/pre-commit`. Confirm it names the installation you just upgraded,
   and that the line does not carry the unpinned-fallback warning. Re-run
   `gitreins install` from the intended installation if it does not.
7. **Config survived, and new keys exist** — `gitreins init` has historically
   overwritten tuned values and, separately, silently *skipped* new sub-keys
   inside an existing section. Diff `.gitreins/config.yaml` against
   `config.yaml.bak` and grep for whatever new keys the release ships. Known
   history: v0.7.x inits scrambled `on:` → `true:` and stripped cap quoting;
   v0.8.0+ inits regenerate from scratch; the backup is single-generation, so a
   second `init` destroys the original — recover from
   `git log -p -- .gitreins/config.yaml` instead.
8. **Guards actually run and block** — `gitreins guard` passes, and a staged
   fake secret is *rejected* by the hook. A non-blocking hook (pre-v0.7.1
   handwritten bash) prints `WARN (non-blocking)` and lets the commit through.
   Note the output vocabulary: the guard prints `✓ secrets — clean`, so a hook
   grepping for the literal `PASS` silently falls through.
9. **Docs/banner agree with the tree** — `python scripts/check_docs_drift.py`
   (also chained into CI at `.github/workflows/ci.yml:81-84`).
10. **Update checker not crying wolf** — a first run may print
    `Update available: <newer>` from a cached `latest_version`; force a fresh read
    rather than trusting the cache.

### 9.4 Upgrading a fleet

The hook pin makes upgrades per-repo, not per-host. `gitreins install` must be
re-run in each repo that pins an old interpreter, and `scripts/check_deployed_surface.py`
is the per-repo gate. Prefer the probe-plus-`md5sum` pair over a version diff, for
the reasons in §5.

---

## 10. Reporting versions honestly — the non-negotiable rule

**Never state a GitReins version from memory, from this document, from a prior
session, or from a shell that may have cached a different binary.** Version
claims are observations, and they expire.

The rule in practice:

1. **Run the command in the current shell, on the host in question, and quote its
   output verbatim.** Then say when you ran it. "`gitreins 0.15.0`, observed via
   `$PIPX_BIN/gitreins --version` at 2026-09-27T06:36Z" is a fact.
   "GitReins 0.15.0" is a recollection.
2. **Name the absolute path and the interpreter**, not just the number. A number
   without a path is uninterpretable — §3 is the proof.
3. **State which surface you measured and which you did not.** Installed is not
   source; source is not released. An unknown surface is `(unverified)`.
4. **Never claim a fix is live because it is merged.** Run §4's byte comparison,
   or say explicitly that you did not.
5. **Report `aligned: true` as "the surface probe is aligned"**, never as "the
   deployed build matches the repo". §5 is the proof that those differ.
6. **Do not quote a version out of a cached artifact without saying it is
   cached.** The update-check cache and a shell's command hash are both caches.
   If you reused a value from earlier in the session, re-run it before relying on
   it.
7. **Label the unverifiable.** PyPI's currently published version, a remote tag,
   or another host's install cannot be asserted from a local shell; either
   measure it (read-only `curl` to the registry is fine) or write `(unverified)`
   and date the note.

The failure this rule prevents is not cosmetic. `DF-010`, `GR-GAP-050`,
`GR-GAP-055`, and `DF-013` all cost real ticks, and every one of them is a
version claim that was true when someone formed it and false when someone relied
on it.

---

## 11. Quick reference

```bash
# --- which build is running (never trust a bare --version) ---
which -a gitreins; type -a gitreins
readlink -f "$(which gitreins)"; head -c 200 "$(which gitreins)"   # shebang = interpreter
/abs/path/gitreins --version
/abs/path/python    -m gitreins --version                          # sharpest probe
cat "$(pipx environment --value PIPX_LOCAL_VENVS)/gitreins/lib/python"*/site-packages/gitreins-*.dist-info/direct_url.json

# --- three-way state ---
git -C <repo> describe --tags                       # source
grep '^version' <repo>/pyproject.toml               # declared version
curl -s https://pypi.org/pypi/gitreins/json | grep -o '"version":"[^"]*"' | head -1   # released
cat ~/.cache/gitreins/update-check.json             # cached PyPI check (<=24h old)

# --- is the deployed build really the merged code? ---
python3 scripts/check_deployed_surface.py           # surface + version; ALIGNED != same code
md5sum "<site-packages>/gitreins/cli.py" <repo>/gitreins/cli.py   # the real answer

# --- release state ---
git rev-list --count v<latest>..HEAD                # how far main is ahead
git tag --sort=-v:refname | head                     # local tags
git log -1 --format='%h %ad %s' --date=iso v<latest> # when the tag was cut

# --- what guards a commit ---
grep -n 'gitreins guard' .git/hooks/pre-commit       # the pinned interpreter, not PATH

# --- docs/banner vs tree ---
python scripts/check_docs_drift.py
```

Gate map, for when you need to know what will fail you:

| Gate | File | Fails when |
|---|---|---|
| tag == declared version | `.github/workflows/release.yml:22-40` | tag and `pyproject.toml` disagree |
| publish idempotence | `.github/workflows/release.yml:41-67` | never — skips a re-tag of a published version |
| wheel self-consistency | `.github/workflows/release.yml:74-133` | `engine/version.py` static/missing, `METADATA` != tag, fresh-venv CLI != tag |
| README banner version | `scripts/check_docs_drift.py` Check A | banner version != `pyproject.toml` |
| test-count claims | `scripts/check_docs_drift.py` Check B | any `N tests pass/across/files` claim != live collection (fails closed if unmeasurable) |
| evaluator tool counts | `scripts/check_docs_drift.py` Check C | docs claims != `EVALUATOR_TOOLS` AST count |
| deployed surface | `scripts/check_deployed_surface.py` | a merged subcommand is missing from the deployed CLI, or versions differ |
| docs drift in CI | `.github/workflows/ci.yml:81-84` | delegates to `check_docs_drift.py` |

---

## Not verified here

- Remote tag state (`git tag` on the GitHub and GitLab remotes) — only local tags
  were read. **Unverified.**
- Whether PyPI's published 0.15.0 wheel is byte-identical to `v0.15.0` in this
  checkout — establishing that needs an isolated install, which this task
  disallows. **Unverified.** `release.yml`'s in-CI assertions are evidence the
  workflow *enforces* it, not that this particular artifact passed.
- Which interpreter the pipx copy would import if re-pointed, and the exact
  version inside the Hermes venv named by this repo's hook pin — reading that
  venv's metadata was out of scope. **Unverified.**
- PyPI's full release list (only `info.version` = `0.15.0` was read; the
  per-release map was not parsed). **Unverified** beyond the latest version.
- Observation date for everything in §2 and §4: **2026-09-27 ~06:36Z**. Re-run
  before relying on any of it.
