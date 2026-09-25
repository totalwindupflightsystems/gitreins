"""gitreins serve — local web server for browsing judgment history live.

Serves a dark, mobile-first SPA at http://127.0.0.1:<port>/ plus a JSON API
that reads the repo's judgment stores on EVERY request (live, not a snapshot):

  GET /                          -> HTML viewer
  GET /api/stats                 -> verdict/pass/fail counts
  GET /api/verdicts              -> verdict list (metadata only)
  GET /api/verdicts/<d>/<hash>   -> full verdict record (criteria + evidence)
  GET /api/tasks                 -> board tasks.jsonl
  GET /api/events                -> board events.jsonl
  GET /api/ticks                 -> scheduler tick ledger (optional, host DB)
  GET /api/qa                    -> QA run ledger (worktree fresh|repro|dogfood rows)
  GET /api/verdicts/<d>/<hash>/evidence/<name>
                                 -> worker evidence artifact: brief, driver-log
                                    tail or graded patch (text/plain)

Per-judgment telemetry (JVIEW-006): ``/api/verdicts/<date>/<hash>`` carries a
``usage`` block when ``.gitreins/usage.jsonl`` has rows traceable to that verdict
and ``/api/stats`` an aggregate ``usage`` summary; costs are reported only when
the checkout configures rates (``usage.price_per_1m_input/_output``), never
invented.

The browsed checkout defaults to the repository containing the working
directory; ``--repo <path>`` (see :func:`resolve_workdir`) points the same
server at any other GitReins checkout, so one install can review every
project's judgment history without ``cd``.

The viewer lists BOTH kinds of record in ``.gitreins/history`` (DF-GITREINS-POC-36):
a judge verdict (no ``kind`` key) and a resolution-gate record (``kind:
"resolution"``, carrying its ``band``). ``GET /api/verdicts`` hands a client the
marker so the two are separable, and ``GET /api/stats`` counts JUDGMENTS only —
a resolution band is not a pass/fail and never moves the pass rate.

Security: binds 127.0.0.1 by default; path params are strictly validated;
read-only — the server never writes to the repo.  The API is an unversioned
but stable contract (additive changes only); docs/judgment-viewer.md records
the contract table, the data sources and the exposure decision.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypedDict

from engine import evidence, qa_ledger, usage
from engine.persist import KIND_RESOLUTION
from engine.repo_paths import (
    WorktreeResolutionError,
    board_file_path,
    resolve_worktree_identity,
)

TICKS_DB = os.path.expanduser("~/.hermes/coding-hermes/scheduler.db")
BOARD_NOT_CONFIGURED = (
    "fleet board not configured; run in a fleet-managed checkout or create .coding-hermes/board/"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH_RE = re.compile(r"^[a-f0-9]{4,16}$")


# ── typed API shapes (TypedDict = plain dict at runtime, so behaviour is ─────
# ── identical to the untyped loaders; the types only describe the payloads) ──


class _VerdictRowOptional(TypedDict, total=False):
    """Metadata stamped only on SOME history records — never manufactured.

    ``worktree``/``branch`` appear on verdicts recorded after the first schema;
    ``kind``/``band`` appear on a resolution-gate record (DF-GITREINS-POC-36). A
    judge verdict carries no ``kind`` at all, so its absence is the marker.
    """

    worktree: str
    branch: str
    kind: str
    band: str


class VerdictRow(_VerdictRowOptional):
    """One metadata-only row of ``GET /api/verdicts`` (no criteria/evidence)."""

    date: str
    hash: str
    task_id: str
    title: str
    passed: bool
    n_criteria: int
    tier1_passed: bool | None


class Stats(TypedDict):
    """Counts block of ``GET /api/stats`` (before the repo/provenance fields)."""

    total: int
    passed: int
    failed: int
    pass_rate: int


class QaPayload(TypedDict):
    """Payload of ``GET /api/qa``: the ledger path plus its run rows."""

    ledger: str
    runs: list[dict[str, Any]]


class UsageEntry(TypedDict):
    """Per-judgment telemetry block of ``GET /api/verdicts/<date>/<hash>``."""

    date: str
    hash: str
    evaluated_at: float
    rows: int
    steps: list[str]
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    first_ts: float
    last_ts: float
    model: str
    cost_usd: float | None
    priced: bool


class UsageSummary(TypedDict):
    """Aggregate telemetry block of ``GET /api/stats``."""

    judgements: int
    verdicts: int
    unattributed: int
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    cost_usd: float
    priced: int
    unpriced: int
    model: str
    prices_configured: bool


class ServeArgumentError(ValueError):
    """Raised when ``--repo`` names a path that cannot be browsed."""


def resolve_workdir(repo: str | None = None, fallback: str | None = None) -> str:
    """Resolve which checkout the viewer browses.

    Without ``--repo`` the viewer keeps its original behaviour: the checkout
    containing the working directory (``fallback``, normally the caller's
    ``get_workdir()``).  With ``--repo <path>`` the path is expanded and must
    be an existing directory; when it sits inside a Git work tree it is
    normalized to that work tree's root, because ``.gitreins/`` and
    ``.coding-hermes/board/`` live there.  A directory that is not inside a
    Git repository is used as given, so a checkout holding only judgment
    history stays browsable.
    """
    if not repo:
        return fallback or os.getcwd()
    path = os.path.abspath(os.path.expanduser(repo))
    if not os.path.isdir(path):
        raise ServeArgumentError(f"--repo is not a directory: {path}")
    try:
        identity = resolve_worktree_identity(path)
    except WorktreeResolutionError:
        return path
    return str(identity.worktree_root)


# ── loaders (read from disk on every request => live) ────────────────────────


def _verdict_dir(workdir: str) -> str:
    return os.path.join(workdir, ".gitreins", "history")


def list_verdicts(workdir: str) -> list[VerdictRow]:
    """Every verdict under ``workdir`` as a metadata-only row (oldest day first)."""
    hist = _verdict_dir(workdir)
    out: list[VerdictRow] = []
    if not os.path.isdir(hist):
        return out
    for day in sorted(os.listdir(hist)):
        ddir = os.path.join(hist, day)
        if not _DATE_RE.match(day) or not os.path.isdir(ddir):
            continue
        for h in sorted(os.listdir(ddir)):
            vpath = os.path.join(ddir, h, "verdict.json")
            if not _HASH_RE.match(h) or not os.path.isfile(vpath):
                continue
            try:
                with open(vpath) as fh:
                    v = json.load(fh)
            except Exception:
                continue
            stages = v.get("stages") or {}
            t1 = stages.get("tier1") or {}
            t2 = stages.get("tier2") or {}
            items = v.get("items") or t2.get("items") or []
            row: VerdictRow = {
                "date": day,
                "hash": h,
                "task_id": v.get("task_id", "?"),
                "title": v.get("task_title", ""),
                "passed": bool(v.get("passed")),
                "n_criteria": len(items),
                "tier1_passed": t1.get("passed") if t1 else None,
            }
            # Metadata was added after the first verdict schema; omit it from
            # legacy list rows rather than manufacturing values for old data.
            if "worktree" in v:
                row["worktree"] = v["worktree"]
            if "branch" in v:
                row["branch"] = v["branch"]
            # DF-GITREINS-POC-36: a resolution-gate record says what it is, so a
            # client never has to tell it apart from a judgment by guessing from
            # the missing criteria. Judge verdicts carry no kind => no key here.
            if v.get("kind"):
                row["kind"] = v["kind"]
            if v.get("band"):
                row["band"] = v["band"]
            out.append(row)
    return out


def load_verdict(workdir: str, date: str, h: str) -> dict[str, Any] | None:
    """Full verdict record for a date/hash pair, or ``None`` when it is unknown."""
    if not _DATE_RE.match(date) or not _HASH_RE.match(h):
        return None
    vpath = os.path.join(_verdict_dir(workdir), date, h, "verdict.json")
    if not os.path.isfile(vpath):
        return None
    try:
        with open(vpath) as fh:
            return json.load(fh)
    except Exception:
        return None


def load_verdict_evidence(workdir: str, date: str, h: str, name: str) -> tuple[str, str] | None:
    """``(filename, text)`` for one evidence artifact of a verdict, else ``None``.

    The artifact must be declared by the verdict's own manifest (see
    :mod:`engine.evidence`), so an unknown ``name`` is a 404 and the served path
    can never leave the verdict directory.
    """
    verdict = load_verdict(workdir, date, h)
    if verdict is None:
        return None
    return evidence.read_evidence(os.path.join(_verdict_dir(workdir), date, h), verdict, name)


def board_status(workdir: str) -> dict[str, Any]:
    """Whether the browsed checkout carries a coding-hermes fleet board.

    ``.coding-hermes/board/`` is a Hermes scheduler artifact: a plain
    ``pip install gitreins`` + ``install``/``init`` checkout has none, and
    most of gitreins works without one.  The board routes keep their
    documented contract in that case (``200`` with an empty list — never a
    ``500``, and never a ``404`` that would blank the viewer, whose boot
    fetches every ``/api/*`` route in one ``Promise.all``).  This block is how
    a client tells "no board here" from "board is empty".
    """
    path = os.path.join(os.path.abspath(workdir), ".coding-hermes", "board")
    configured = os.path.isdir(path)
    return {
        "configured": configured,
        "path": path,
        "message": "" if configured else BOARD_NOT_CONFIGURED,
    }


def load_jsonl(workdir: str, name: str, limit: int = 2000) -> list[Any]:
    """Last ``limit`` parses of board file ``name`` (rows are any JSON value)."""

    try:
        path = board_file_path(workdir, name)
    except (WorktreeResolutionError, OSError, ValueError):
        # A browsed checkout (--repo) may hold judgments but no coding-hermes
        # board: an absent board is an empty list, never a 500.  The same
        # holds for a checkout that has no board directory at all (a plain
        # `pip install gitreins` repo) — see board_status() for how a client
        # tells "no board here" from "board is empty"; the routes stay 200 so
        # the viewer's parallel fetch cannot blank the page.
        return []
    rows: list[Any] = []
    if os.path.isfile(path):
        with open(path) as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def load_ticks(project: str, limit: int = 500) -> list[dict[str, Any]]:
    """Tick rows for ``project`` from the host scheduler ledger (read-only)."""
    if not os.path.exists(TICKS_DB):
        return []
    try:
        db = sqlite3.connect(f"file:{TICKS_DB}?mode=ro", uri=True)
        rows = db.execute(
            "SELECT id, spawned_at, status, outcome, commits, files_changed,"
            " round(cost_usd,2), substr(coalesce(error,''),1,160)"
            " FROM ticks WHERE project_name=? ORDER BY spawned_at DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        db.close()
    except Exception:
        return []
    keys = ["id", "spawned_at", "status", "outcome", "commits", "files", "cost", "error"]
    return [dict(zip(keys, r)) for r in rows]


def load_qa(workdir: str, limit: int = 200) -> QaPayload:
    """QA run ledger rows (oldest-first, newest ``limit`` kept) plus its path.

    A missing, unreadable or half-garbage ledger is an empty run list, never a
    500 — the same contract as :func:`load_ticks`.
    """
    ledger = qa_ledger.qa_ledger_path(workdir)
    try:
        rows = qa_ledger.list_rows(workdir, path=ledger)[-limit:]
    except Exception:
        return {"ledger": ledger, "runs": []}
    return {"ledger": ledger, "runs": rows}


def graded_verdicts(verdicts: list[VerdictRow]) -> list[VerdictRow]:
    """The rows that carry a pass/fail JUDGMENT (not resolution-gate records).

    Both kinds live in the same history store (DF-GITREINS-POC-36), but a
    resolution record has no ``passed`` — counting one as a failed judgment
    would inflate the failure rate the stats header exists to report.
    """
    return [v for v in verdicts if v.get("kind") != KIND_RESOLUTION]


def stats(verdicts: list[VerdictRow]) -> Stats:
    """Counts block for ``GET /api/stats`` (``pass_rate`` is an integer percent).

    Resolution-gate records are excluded: they stay listable through
    ``GET /api/verdicts``, and the header keeps counting judgments only.
    """
    graded = graded_verdicts(verdicts)
    n_pass = sum(1 for v in graded if v["passed"])
    return {
        "total": len(graded),
        "passed": n_pass,
        "failed": len(graded) - n_pass,
        "pass_rate": round(100 * n_pass / len(graded)) if graded else 0,
    }


# ── per-judgment telemetry (JVIEW-006) ───────────────────────────────────────


def verdict_stamps(workdir: str) -> list[tuple[str, str, float]]:
    """``(date, hash, evaluated_at_epoch)`` for every verdict that records one."""
    hist = _verdict_dir(workdir)
    stamps: list[tuple[str, str, float]] = []
    if not os.path.isdir(hist):
        return stamps
    for day in sorted(os.listdir(hist)):
        ddir = os.path.join(hist, day)
        if not _DATE_RE.match(day) or not os.path.isdir(ddir):
            continue
        for h in sorted(os.listdir(ddir)):
            vpath = os.path.join(ddir, h, "verdict.json")
            if not _HASH_RE.match(h) or not os.path.isfile(vpath):
                continue
            try:
                with open(vpath) as fh:
                    evaluated_at = json.load(fh).get("evaluated_at")
            except Exception:
                continue
            epoch = _epoch(evaluated_at)
            if epoch is not None:
                stamps.append((day, h, epoch))
    return stamps


def _epoch(value: Any) -> float | None:
    """Epoch seconds for an ISO-8601 stamp; naive stamps are read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_usage(workdir: str) -> tuple[dict[str, UsageEntry], UsageSummary]:
    """Attributed usage per verdict plus the aggregate the stats header shows.

    Attribution is by time (the telemetry carries no task id) and each line is
    charged to at most one verdict — see :mod:`engine.usage`.
    """
    prices = usage.load_price_config(workdir)
    rows = usage.load_usage_rows(workdir)
    index = usage.attribute_rows(verdict_stamps(workdir), rows, prices)
    total = len(list_verdicts(workdir))
    return index, usage.summarize(index, prices, total_verdicts=total)


# ── HTML viewer (hash-route SPA; fetches /api/* live) ───────────────────────

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitReins — Judgment Browser</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:#0f0f1a;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:12px;line-height:1.5}
h1{font-size:clamp(22px,6vw,36px);background:linear-gradient(135deg,#f7971e,#ffd200);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:#8a8aa3;font-size:13px;margin:2px 0 14px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media(min-width:600px){.grid{grid-template-columns:repeat(5,1fr)}body{padding:20px}}
.stat{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;padding:10px;text-align:center}
.stat .n{font-size:clamp(19px,5vw,26px);font-weight:800;color:#4ade80;display:block}
.stat .l{font-size:10px;color:#8a8aa3;text-transform:uppercase;letter-spacing:.5px}
.stat .u{font-size:10px;color:#5a5a75;display:block;margin-top:2px}
.tabs{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;align-items:center}
.tab{background:#12121f;border:1px solid #2a2a3e;border-radius:20px;padding:5px 14px;font-size:12px;cursor:pointer;color:#8a8aa3}
.tab.active{background:#166534;color:#4ade80;border-color:#166534}
.tab input{background:#12121f;border:1px solid #2a2a3e;border-radius:16px;color:#e0e0e0;padding:5px 12px;font-size:12px;width:150px;outline:none}
.row{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;padding:10px 12px;margin-bottom:7px;cursor:pointer}
.row:hover{border-color:#4a4a6a}
.top{display:flex;justify-content:space-between;gap:8px;align-items:baseline;flex-wrap:wrap}
.task{font-weight:700;color:#60a5fa;font-size:13px}
.title{color:#c4c4d8;font-size:12px;flex:1;min-width:120px}
.badge{padding:2px 9px;border-radius:20px;font-size:10px;font-weight:800;white-space:nowrap}
.pass{background:#166534;color:#4ade80}.fail{background:#3b0820;color:#f472b6}
.mute{background:#12121f;color:#8a8aa3}
.warn{background:#3b1e08;color:#f59e0b}
.meta{color:#5a5a75;font-size:10.5px;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
h2{font-size:16px;color:#ffd200;margin:18px 0 8px}
.panel{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:12px;padding:10px 12px;max-height:420px;overflow-y:auto}
.ev{border-bottom:1px solid #1f1f33;padding:6px 2px;font-size:11.5px;display:flex;gap:8px;flex-wrap:wrap}
.ts{color:#5a5a75;font-family:ui-monospace,Menlo,monospace;white-space:nowrap}
.t{color:#a78bfa;min-width:90px}.k{color:#60a5fa}.v{color:#c4c4d8}
#detail{display:none;background:#12121f;border:1px solid #4a4a6a;border-radius:12px;padding:14px;margin:10px 0}
#detail h3{color:#ffd200;font-size:15px;margin-bottom:6px}
.crit{margin:8px 0;padding:8px 10px;background:#1a1a2e;border-radius:8px;border-left:3px solid #2a2a3e}
.crit.p{border-left-color:#4ade80}.crit.f{border-left-color:#f472b6}
.crit .c{font-size:12.5px;font-weight:600}.crit .d{font-size:11.5px;color:#9a9ab3;margin-top:4px;white-space:pre-wrap}
pre{background:#0a0a14;border:1px solid #2a2a3e;border-radius:8px;padding:10px;font-size:10.5px;overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0;white-space:pre-wrap}
.sec{color:#60a5fa;font-size:12px;text-transform:uppercase;letter-spacing:.5px;margin:12px 0 4px}
.close{float:right;background:#2a2a3e;color:#e0e0e0;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
footer{text-align:center;color:#5a5a75;font-size:10.5px;padding:14px 0 6px}
a{color:#60a5fa}
</style></head><body>
<h1>⚖️ Judgment Browser</h1>
<div class="sub" id="sub"></div>
<div class="grid" id="stats"></div>
<div class="tabs">
<div class="tab active" data-f="all">All</div>
<div class="tab" data-f="pass">PASS</div>
<div class="tab" data-f="fail">FAIL</div>
<div class="tab"><input id="q" placeholder="search task…"></div>
<div class="tab mute" id="livehint" title="Data is re-read from disk on every request — refresh to see new judgments">live · refresh for new</div>
</div>
<div id="list"></div>
<div id="detail"></div>
<h2>🕰️ Board Event Timeline</h2><div class="panel" id="evlist"></div>
<h2>🖥️ Scheduler Ticks</h2><div class="panel" id="ticklist"></div>
<h2>🧪 QA Runs</h2><div class="panel" id="qalist"></div>
<footer>gitreins serve · live reads from .gitreins/history + board + scheduler ledger + QA ledger</footer>
<script>
const esc=s=>{const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML};
const badge=v=>v?'<span class="badge pass">PASS</span>':'<span class="badge fail">FAIL</span>';
const kindBadge=v=>v.kind==='resolution'?'<span class="badge mute">'+esc(v.band||'resolution')+'</span>':badge(v.passed);
let V=[],filter='all',q='',CUR=null;
async function j(url){const r=await fetch(url);if(!r.ok)throw new Error(url);return r.json()}
async function boot(){
  const [st,vs,ev,tk,qa]=await Promise.all([j('/api/stats'),j('/api/verdicts'),j('/api/events'),j('/api/ticks'),j('/api/qa')]);
  V=vs.verdicts;
  document.getElementById('sub').textContent=st.repo+' · live view · '+st.generated;
  document.getElementById('sub').title=st.path||'';
  document.getElementById('stats').innerHTML=
    '<div class="stat"><span class="n">'+st.total+'</span><span class="l">Judgments</span></div>'+
    '<div class="stat"><span class="n">'+st.passed+'</span><span class="l">Passed</span></div>'+
    '<div class="stat"><span class="n">'+st.failed+'</span><span class="l">Failed (gates held)</span></div>'+
    '<div class="stat"><span class="n">'+st.pass_rate+'%</span><span class="l">Pass rate</span></div>'+
    spendCard(st.usage);
  render();
  document.getElementById('evlist').innerHTML=(ev.events||[]).slice().reverse().map(e=>{
    let d={};try{d=JSON.parse(e.detail||'{}')}catch(_){}
    return '<div class="ev"><span class="ts">'+esc((e.timestamp||'').slice(0,16))+'</span><span class="t">'+
    esc(e.event_type)+'</span><span class="k">'+esc(e.task_id||'')+'</span><span class="v">'+
    (d.commit?esc(String(d.commit).slice(0,8))+' ':'')+esc(d.verdict||'')+(d.tick?' · tick '+esc(d.tick):'')+'</span></div>';
  }).join('');
  document.getElementById('ticklist').innerHTML=(tk.ticks||[]).map(t=>{
    const ok=t.status=='completed'&&t.outcome=='committed';
    const dot=ok?'🟢 committed':(t.status=='completed'?'🟡 '+esc(t.outcome||t.status):'🔴 '+esc(t.status));
    return '<div class="ev"><span class="ts">'+esc((t.spawned_at||'').slice(0,16))+'</span><span class="t">'+dot+
    '</span><span class="v">'+(t.commits||0)+' commits · '+(t.files||0)+' files · $'+esc(t.cost==null?'0':t.cost)+
    (t.error?' · '+esc(t.error):'')+'</span></div>';
  }).join('')||'<p style="color:#5a5a75;font-size:12px">'+(tk.project?'no scheduler ticks recorded for '+esc(tk.project):'no scheduler project selected (start with --project <name>)')+'</p>';
  document.getElementById('qalist').innerHTML=(qa.runs||[]).map(r=>{
    const v=String(r.verdict||'').toUpperCase();
    const cls=v==='PASS'?'pass':(v==='FAIL'?'fail':'mute');
    const icon=v==='PASS'?'\u2705':(v==='FAIL'?'\u274c':'\u00b7');
    const cells=r.cells&&typeof r.cells==='object'?Object.values(r.cells):[];
    const np=cells.filter(c=>['pass','passed','ok'].includes(String(c).toLowerCase())).length;
    const nf=cells.filter(c=>['fail','failed','error'].includes(String(c).toLowerCase())).length;
    const graded=np+nf;
    const cellsTxt=graded?('cells '+np+'/'+graded+' passed'):'no graded cells';
    const bits=[];
    if(typeof r.exit_code==='number')bits.push('exit '+r.exit_code);
    if(r.commit)bits.push('commit '+esc(String(r.commit).slice(0,7)));
    return '<div class="ev"><span class="ts">'+esc((r.ts||'').slice(0,16))+'</span>'+
    '<span class="badge '+cls+'">'+icon+' '+esc(v||'?')+'</span><span class="t">'+esc(r.kind||'?')+
    '</span><span class="v">'+esc(cellsTxt)+(bits.length?' \u00b7 '+bits.join(' \u00b7 '):'')+'</span></div>';
  }).join('')||'<p style="color:#5a5a75;font-size:12px">no QA runs recorded (ledger: '+esc(qa.ledger||'')+')</p>';
  if((qa.runs||[]).length)document.getElementById('qalist').innerHTML+='<p style="color:#5a5a75;font-size:12px">ledger: '+esc(qa.ledger||'')+'</p>';
}
function money(usd){return '$'+(Number(usd)||0).toFixed(2)}
function costBadge(v){
  const u=v.usage;if(!u)return '';
  if(u.priced)return ' <span class="badge mute" title="judge cost from usage.jsonl at the rates configured in .gitreins/config.yaml">$'+Number(u.cost_usd).toFixed(4)+'</span>';
  return ' <span class="badge warn" title="usage.jsonl rows are traceable, but no price is configured">cost unpriced</span>';
}
function spendCard(u){
  if(!u)return '<div class="stat"><span class="n">—</span><span class="l">Judge spend</span></div>';
  const priced=u.prices_configured&&u.priced>0;
  const head=priced?money(u.cost_usd):'unpriced';
  const sub=priced
    ? (u.priced+' priced · '+u.unpriced+' unpriced')
    : 'tokens only · set usage.price_per_1m_input/_output';
  return '<div class="stat" title="'+esc(sub)+'"><span class="n">'+head+'</span><span class="l">Judge spend</span>'+
    '<span class="u">'+u.judgements+'/'+u.verdicts+' judgments · '+
    Math.round((u.tokens_in||0)/1000)+'k in / '+Math.round((u.tokens_out||0)/1000)+'k out'+
    (u.unattributed?' · '+u.unattributed+' no telemetry':'')+'</span></div>';
}
function telemetry(v){
  const u=v.usage;if(!u)return '';
  const cost=u.priced?('$'+Number(u.cost_usd).toFixed(4)+' · '+esc(u.model||'configured rates')):'unpriced (cost needs usage.price_per_1m_input/_output)';
  return '<div class="sec">Judge telemetry</div><div class="meta">'+u.rows+' line(s) from usage.jsonl · '+
    (u.tokens_in||0)+' tokens in / '+(u.tokens_out||0)+' out'+
    ((u.cache_read||0)?' · '+u.cache_read+' cache read':'')+
    (u.steps&&u.steps.length?' · steps: '+esc(u.steps.join(', ')):'')+'</div>'+
    '<div class="meta">cost: '+cost+'</div>';
}
function render(){
  const rows=V.filter(v=>(filter==='all'||(v.kind!=='resolution'&&(filter==='pass')===v.passed))&&(!q||(v.task_id+' '+v.title).toLowerCase().includes(q)));
  document.getElementById('list').innerHTML=rows.map(v=>{
    const origin=[v.worktree?'worktree: '+v.worktree:'',v.branch?'branch: '+v.branch:''].filter(Boolean).join(' · ');
    return '<div class="row" onclick="show(\\''+v.date+'\\',\\''+v.hash+'\\')">'+
    '<div class="top"><span class="task">'+esc(v.task_id)+'</span><span class="title">'+esc(v.title)+'</span>'+kindBadge(v)+'</div>'+
    '<div class="meta">'+v.date+' · '+v.hash+' · '+esc(v.kind==='resolution'?'resolution gate record':(v.n_criteria+' criteria · tier1: '+(v.tier1_passed==null?'—':(v.tier1_passed?'PASS':'FAIL'))))+'</div>'+
    (origin?'<div class="meta">'+esc(origin)+'</div>':'')+'</div>';
  }).join('')
    ||'<p style="color:#5a5a75;font-size:13px">no judgments match</p>';
}
async function show(date,hash){
  const r=await fetch('/api/verdicts/'+date+'/'+hash);if(!r.ok)return;
  const v=await r.json();
  const stages=v.stages||{};const t1=stages.tier1||{};const t2=stages.tier2||{};
  const items=v.items||(t2.items||[]);
  const origin=[v.worktree?'worktree: '+v.worktree:'',v.branch?'branch: '+v.branch:''].filter(Boolean).join(' · ');
  const d=document.getElementById('detail');
  d.innerHTML='<button class="close" onclick="document.getElementById(\\'detail\\').style.display=\\'none\\'">✕ close</button>'+
   '<h3>'+esc(v.task_id||'?')+' — '+esc(v.task_title||'')+'</h3>'+
   '<div style="color:#8a8aa3;font-size:11px">'+date+' · '+hash+' · '+(v.kind==='resolution'?('resolution gate · band '+esc(v.band||'?')):('overall '+(v.passed?'PASS':'FAIL')))+
   costBadge(v)+'</div>'+
   (origin?'<div class="meta">'+esc(origin)+'</div>':'')+
   '<div class="sec">Criteria ('+items.length+')</div>'+
   (items.map(it=>'<div class="crit '+(it.status=='PASS'?'p':'f')+'"><div class="c">'+(it.status=='PASS'?'✅':'❌')+' '+esc(it.criterion)+'</div><div class="d">'+esc(it.detail)+'</div></div>').join('')
   ||'<p style="color:#5a5a75;font-size:12px">no per-criterion items recorded</p>')+
   ((t1.summary)?'<div class="sec">Tier 1 — static gates</div><pre>'+esc(t1.summary)+'</pre>':'')+
   ((t2.summary)?'<div class="sec">Tier 2 — judge summary</div><pre>'+esc(t2.summary)+'</pre>':'')+
   (v.summary?'<div class="sec">Verdict summary</div><pre>'+esc(v.summary)+'</pre>':'')+
   telemetry(v)+
   evidenceSection(v);
  CUR={date:date,hash:hash,items:(v.evidence&&v.evidence.items)||[]};
  d.style.display='block';d.scrollIntoView({behavior:'smooth',block:'start'});
}
function evidenceSection(v){
  const ev=(v.evidence&&v.evidence.items)||[];
  if(!ev.length)return '<div class="sec">Evidence</div><p style="color:#5a5a75;font-size:12px">no worker evidence recorded for this verdict (the brief, driver-log tail and graded patch are collected at task complete)</p>';
  return '<div class="sec">Evidence ('+ev.length+')</div>'+ev.map((it,i)=>
    '<div class="ev"><span class="t">'+esc(it.label||it.name)+'</span><span class="v">'+(it.bytes||0)+' B'+
    (it.truncated?' \u00b7 truncated':'')+(it.source?' \u00b7 '+esc(it.source):'')+
    '</span><button class="close" style="float:none" onclick="evload('+i+')">load</button>'+
    '<pre id="ev'+i+'" style="display:none"></pre></div>').join('');
}
async function evload(i){
  const it=CUR&&CUR.items?CUR.items[i]:null;if(!it)return;
  const pre=document.getElementById('ev'+i);if(!pre)return;
  if(pre.style.display==='block'){pre.style.display='none';return;}
  const r=await fetch('/api/verdicts/'+CUR.date+'/'+CUR.hash+'/evidence/'+encodeURIComponent(it.name));
  pre.textContent=r.ok?await r.text():'unavailable';pre.style.display='block';
}
document.querySelectorAll('.tab[data-f]').forEach(t=>t.onclick=()=>{document.querySelectorAll('.tab[data-f]').forEach(x=>x.classList.remove('active'));t.classList.add('active');filter=t.dataset.f;render()});
document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase();render()};
boot();
</script></body></html>"""


# ── HTTP server ──────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    # Class-level config: ``serve()`` sets both before the socket is bound.
    workdir: str = "."
    project: str = ""

    def _send(
        self, code: int, body: bytes, ctype: str, extra_headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/stats":
                vs = list_verdicts(self.workdir)
                _index, usage_summary = load_usage(self.workdir)

                self._json(
                    {
                        **stats(vs),
                        "usage": usage_summary,
                        "board": board_status(self.workdir),
                        "repo": os.path.basename(os.path.abspath(self.workdir)),
                        "path": os.path.abspath(self.workdir),
                        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    }
                )
            elif path == "/api/verdicts":
                self._json({"verdicts": list_verdicts(self.workdir)})
            elif path.startswith("/api/verdicts/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 6 and parts[4] == "evidence":
                    # Worker evidence artifact (JVIEW-005). The manifest decides
                    # what exists, so a name the verdict does not declare is a
                    # 404 and no unlisted file is ever served.
                    artifact = load_verdict_evidence(self.workdir, parts[2], parts[3], parts[5])
                    if artifact is None:
                        self._json({"error": "not found"}, 404)
                        return
                    filename, text = artifact
                    self._send(
                        200,
                        text.encode(),
                        "text/plain; charset=utf-8",
                        extra_headers={"X-Gitreins-Evidence": filename},
                    )
                    return
                if len(parts) != 4:
                    self._json({"error": "use /api/verdicts/<date>/<hash>"}, 400)
                    return
                v = load_verdict(self.workdir, parts[2], parts[3])
                if v is None:
                    self._json({"error": "not found"}, 404)
                    return
                # Per-judgment telemetry is joined on the way out (JVIEW-006):
                # the stored verdict.json is served verbatim otherwise, and a
                # verdict with no traceable usage rows simply has no block.
                index, _summary = load_usage(self.workdir)
                entry = index.get(f"{parts[2]}/{parts[3]}")
                if entry:
                    v = {**v, "usage": entry}
                self._json(v)
            elif path == "/api/tasks":
                self._json({"tasks": load_jsonl(self.workdir, "tasks.jsonl")})
            elif path == "/api/events":
                self._json({"events": load_jsonl(self.workdir, "events.jsonl")})
            elif path == "/api/ticks":
                # Host-coupled and opt-in: one scheduler DB per machine, and
                # only the project named by --project has ticks to show.
                self._json(
                    {
                        "project": self.project or None,
                        "ticks": load_ticks(self.project) if self.project else [],
                    }
                )
            elif path == "/api/qa":
                self._json(load_qa(self.workdir))
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # keep the server alive on handler bugs
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Quiet by default; the name ``format`` matches http.server's own API.
        if os.environ.get("GITREINS_SERVE_VERBOSE"):
            super().log_message(format, *args)


def serve(
    workdir: str,
    host: str = "127.0.0.1",
    port: int = 8616,
    project: str = "",
    open_browser: bool = False,
) -> None:
    """Serve the judgment browser for ``workdir`` until interrupted.

    ``workdir`` must already be resolved (see :func:`resolve_workdir`);
    ``project`` is the optional scheduler project whose tick ledger is shown.
    """
    Handler.workdir = workdir
    Handler.project = project
    httpd = ThreadingHTTPServer((host, port), Handler)
    # Report the port actually bound (pass --port 0 to let the OS choose one).
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"GitReins judgment browser: {url}")
    print(f"repo: {os.path.abspath(workdir)}  (Ctrl-C to stop)")
    print(
        f"scheduler project: {project if project else '(none - pass --project <name> to show ticks)'}"
    )
    board = board_status(workdir)
    if board["configured"]:
        print(f"board: {board['path']}")
    else:
        print(f"board: {BOARD_NOT_CONFIGURED} ({board['path']})")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"warning: --host {host} serves judgment data over the network with NO "
            "authentication; keep 127.0.0.1 unless the network is trusted"
        )
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
