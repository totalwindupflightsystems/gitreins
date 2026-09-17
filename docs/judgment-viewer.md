# Judgment Viewer (`gitreins serve`)

`gitreins serve` starts a read-only local web server that renders the judgment
history of a checkout: every verdict in `.gitreins/history/`, the criteria and
Tier 1/Tier 2 evidence inside each verdict, the worker evidence embedded next to
the verdict (brief, driver-log tail, graded patch), the board event timeline, the
QA run ledger, and (when asked for) the scheduler tick ledger for a project.

It exists because `.gitreins/history/<date>/<hash>/verdict.json` is a durable
audit record that nobody wants to read as JSON. The browser answers the three
questions a review actually asks — what was judged, what did the gates say, and
what did the judge say — without leaving the terminal far behind.

```
gitreins serve
```

Then open <http://127.0.0.1:8616/>. Ctrl-C stops it. Nothing is written: the
server never mutates the repository it browses.

## Usage

```
gitreins serve [--repo <path>] [--port <port>] [--host <host>] [--project <name>] [--open]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--repo` | the repository you run from | Browse another checkout's judgment history by path — no `cd` needed |
| `--port` | `8616` | Port to bind; `0` binds an ephemeral port and the banner prints the real one |
| `--host` | `127.0.0.1` | Bind address. Anything other than loopback serves judgment data over the network — see [Security model](#security-model) |
| `--project` | none | Scheduler project name whose tick ledger is shown (e.g. `gitreins-poc`) |
| `--open` | off | Open the browser automatically after binding |

### Browsing another project

One install can review every project on the machine:

```
gitreins serve --repo ~/other-project --port 8660
gitreins serve --repo /srv/work/checkout --project my-project
```

`--repo` accepts:

- the root of any GitReins checkout (the checkout containing `.gitreins/`);
- **any path inside it** — the path is normalized to that Git work tree's root,
  because `.gitreins/history/` and `.coding-hermes/board/` live there;
- a plain directory that is not a Git repository at all, as long as it holds
  `.gitreins/history/` — useful for archived or copied history.

A path that does not exist, or that is a file rather than a directory, is
refused before the server binds: `error: --repo is not a directory: <path>`
with exit code `2`.

A browsed checkout that has judgments but no `.coding-hermes/board/` is fully
browsable: `/api/tasks` and `/api/events` answer with empty lists instead of an
error.

## API contract

Every response is JSON except `/`, which is the HTML viewer. The API is
**unversioned but stable**: changes are additive, existing fields keep their
meaning, and a breaking change will arrive as a new endpoint rather than a
changed one. Data is re-read from disk on every request, so the contract is
"current state of the checkout", never a snapshot taken at startup.

| Method | Path | Success | Payload | Errors |
|--------|------|---------|---------|--------|
| GET | `/` | 200 HTML | the single-page viewer (no server-side data; it fetches `/api/*`) | — |
| GET | `/api/stats` | 200 | `total`, `passed`, `failed`, `pass_rate`, `repo`, `path`, `generated` | — |
| GET | `/api/verdicts` | 200 | `{"verdicts": [row, …]}` — metadata only, newest last | — |
| GET | `/api/verdicts/<date>/<hash>` | 200 | the full `verdict.json` (criteria, `stages.tier1`, `stages.tier2`, `evidence` manifest when one was collected) | `400` malformed path (not `<date>/<hash>`), `404` unknown date/hash |
| GET | `/api/verdicts/<date>/<hash>/evidence/<name>` | 200 `text/plain` | one worker-evidence artifact declared by that verdict's `evidence` manifest (`brief`, `log`, `patch`) | `400` missing `<name>`, `404` unknown verdict or a name the manifest does not declare (including an artifact deleted since) |
| GET | `/api/tasks` | 200 | `{"tasks": [row, …]}` from the board's `tasks.jsonl` | `200 []` when the board is absent |
| GET | `/api/events` | 200 | `{"events": [row, …]}` from the board's `events.jsonl` | `200 []` when the board is absent |
| GET | `/api/ticks` | 200 | `{"project": <name or null>, "ticks": [row, …]}` | `200 []` when `--project` is unset or the ledger is unavailable |
| GET | `/api/qa` | 200 | `{"ledger": <path>, "runs": [row, …]}` from the QA run ledger, oldest first | `200 []` when the ledger is absent or unreadable |
| any | other path | — | `{"error": "not found"}` | `404` |

Verdict list rows carry `date`, `hash`, `task_id`, `title`, `passed`,
`n_criteria`, `tier1_passed`, plus `worktree`/`branch` when the verdict recorded
them. Rows are omitted from the list (not zero-filled) when a field predates the
schema — the viewer never invents values for legacy records.

The SPA is a hash-free, single-page app: it loads `/api/stats`, `/api/verdicts`,
`/api/events`, `/api/ticks` and `/api/qa` once, then opens a verdict via
`/api/verdicts/<date>/<hash>` when a row is clicked. Refresh for new judgments;
there is no push channel.

## Worker evidence in a verdict directory

`task complete` copies the run's own artifacts next to `verdict.json`, because a
verdict that names a commit but not the brief, the driver log and the patch it
graded is only half an audit record — and those sources usually live in `/tmp`
and die with the tick.

| Artifact | Source | Bound |
|----------|--------|-------|
| `worker-brief.md` | `GITREINS_WORKER_BRIEF` (path), else `<checkout>/.gitreins/worker-brief.md` | first 32 KiB, head kept |
| `driver-log.tail.txt` | `GITREINS_DRIVER_LOG` (path) | last 16 KiB, tail kept |
| `commit.patch` | the patch of the commit the verdict stamped — the fix as landed (`git show <stamped commit>`, else `git show HEAD`) | first 256 KiB, head kept |
| `worktree.patch` | `git diff HEAD` — whatever was uncommitted when the verdict was written, i.e. what the judge read | first 256 KiB, head kept |

Each artifact is listed in `verdict.json → evidence.items` with its `name`,
`label`, `file`, `bytes`, `truncated` flag and the `source` it was copied from,
and the viewer's detail pane renders that list as the **Evidence** section
(load-on-click, so a large patch is fetched only when asked for). A clipped
artifact says so: `truncated: true` and a `<N> of <M> bytes dropped` line at the
clip point, naming the arithmetic instead of silently losing text.

Three rules make the section trustworthy:

- **The landed fix and the graded tree are separate artifacts.** `commit.patch`
  is the patch of the commit the verdict stamped; `worktree.patch` is the
  uncommitted diff (`git diff HEAD`), recorded only when there is one. A
  checkout that is never clean — generated files, graph caches — would otherwise
  store that noise under the name "the fix".
- **Absent means absent.** A source that is missing, unreadable or empty is left
  out of the manifest entirely — the pane says the artifact was not recorded
  rather than showing an empty file that reads as "the worker wrote nothing".
- **Evidence never fails a verdict.** Collection is best-effort: a collector
  error, an unwritable history directory or an unreadable source leaves the
  verdict untouched. A verdict recorded before this feature exists simply has no
  `evidence` block.

Only names declared in the verdict's own manifest are servable, and a declared
name must be a plain file name (no separators), so the artifact route cannot be
used to read anything outside the verdict directory.

## Data sources

| Surface | Source | Absent source | Notes |
|---------|--------|---------------|-------|
| Verdict list + detail | `<checkout>/.gitreins/history/<YYYY-MM-DD>/<hash>/verdict.json` | `total: 0`, empty list | Filesystem only. Unparseable or non-matching entries are skipped, never guessed |
| Worker evidence | the same verdict directory: `worker-brief.md`, `driver-log.tail.txt`, `commit.patch` | the pane says the artifact was not recorded | Written by `task complete` (see [Worker evidence](#worker-evidence-in-a-verdict-directory)); served only for names the verdict's own `evidence` manifest declares |
| Board timeline | `<canonical>/.coding-hermes/board/events.jsonl` | `[]` | Resolved through Git's common dir, so a linked worktree shows the shared board |
| Board tasks | `<canonical>/.coding-hermes/board/tasks.jsonl` | `[]` | Last 2000 lines are read |
| Ticks | `~/.hermes/coding-hermes/scheduler.db`, table `ticks`, filtered by `project_name` | `[]` | Host-coupled, read-only SQLite, opt-in per `--project`; without `--project` the panel reads `no scheduler project selected (start with --project <name>)`, and a selected project with no ledger rows reads `no scheduler ticks recorded for <project>` |
| QA runs | `<repo>/.gitreins/qa-ledger.jsonl`, overridable by `GITREINS_QA_LEDGER` or the `qa_ledger.path` config key | `[]` | Written by `worktree fresh\|repro\|dogfood` and `gitreins qa record`; rows are oldest-first and malformed lines are skipped, never guessed; the panel names the ledger path and shows verdict, cells summary, exit code and commit per run |

`gitreins serve` reads the filesystem; it does **not** fall back to the
`refs/heads/gitreins` verdict branch the way `gitreins report` does. On a fresh
clone with no local history the viewer is legitimately empty — run `gitreins
report` for the branch fallback, or fetch the branch into `.gitreins/`.

## Security model

- **Read-only.** No endpoint writes to the browsed checkout, and the server
  holds no credentials. `--repo` grants nothing beyond what the process could
  already read.
- **Loopback by default.** The bind address is `127.0.0.1`, so the viewer is
  reachable only from the machine it runs on.
- **Strict path validation.** Date and hash path segments must match
  `^\d{4}-\d{2}-\d{2}$` and `^[a-f0-9]{4,16}$` before any file is opened, so
  `..`, absolute paths and encoded traversal (`%2e%2e`) can never escape the
  history directory. This is enforced by construction: the segments are
  validated first, then joined.
- **Evidence artifacts are manifest-bound.** The evidence route serves only a
  name the verdict's own `evidence` block declares, and a declared file name must
  be a plain name — a crafted `<name>` (or a hand-edited manifest trying to name
  `../something`) is a `404`/`None`, never an open of an unlisted path.
- **Board access is name-bound.** Board files are resolved through
  `board_file_path`, which accepts only a direct child filename of the canonical
  board directory.
- **No auth.** There is no token, no cookie, no CORS relaxation.

### Decision: loopback-only is retained; no token auth (2026-09-16)

**Decision.** The default bind address stays `127.0.0.1` and the viewer ships
without authentication. `--host` remains an escape hatch for a trusted network,
and it now warns on stderr that the data leaves the loopback interface with no
authentication.

**Why.** The payload is local quality evidence: verdicts, criteria and gate
output for one checkout. A token without TLS is a false sense of safety
(credentials and content travel in cleartext, and a browser would hold the token
in a URL or localStorage), while TLS for a local tool needs certificate
management that the tool does not have. Loopback-only with an explicit, warned
opt-out is the honest trade: the safe path is the default path.

**Revisit when.** (a) the viewer gains a write action, (b) it must be reachable
from another machine as a supported workflow rather than an experiment, or (c)
verdicts start carrying data that is sensitive beyond the checkout. Any of those
turns "add token auth" into its own board row rather than a flag on this one.

### Decision: the API stays unversioned (2026-09-16)

**Decision.** No `/v1` prefix. The contract is documented here, changes are
additive, and breaking changes get a new endpoint.

**Why.** The only consumers are this SPA and local scripts; the versioned
alternative (dual-serving `/api` and `/api/v1`) buys nothing today and costs
permanent duplication. The doc table above is the contract of record — if that
stops being true, if the API grows external consumers, versioning becomes a
row of its own.

## Static variant

`scripts/judgment_viewer.py` renders the same history to a standalone HTML file
for publishing without a server. Serve is for a live, always-current view;
the static script is for attaching evidence to something.

## See also

- `gitreins report` — the terminal view of the same verdict history, including
  the `refs/heads/gitreins` branch fallback.
- [CLI reference](cli-reference.md) — the full command surface.
- [Disposable verification](disposable-verification.md) — where most verdicts
  under a scratch clone come from.
