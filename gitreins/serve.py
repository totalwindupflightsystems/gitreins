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

Security: binds 127.0.0.1 by default; path params are strictly validated;
read-only — the server never writes to the repo.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.repo_paths import board_file_path

TICKS_DB = os.path.expanduser("~/.hermes/coding-hermes/scheduler.db")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH_RE = re.compile(r"^[a-f0-9]{4,16}$")


# ── loaders (read from disk on every request => live) ────────────────────────


def _verdict_dir(workdir: str) -> str:
    return os.path.join(workdir, ".gitreins", "history")


def list_verdicts(workdir: str) -> list[dict]:
    hist = _verdict_dir(workdir)
    out = []
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
                v = json.load(open(vpath))
            except Exception:
                continue
            stages = v.get("stages") or {}
            t1 = stages.get("tier1") or {}
            t2 = stages.get("tier2") or {}
            items = v.get("items") or t2.get("items") or []
            out.append(
                {
                    "date": day,
                    "hash": h,
                    "task_id": v.get("task_id", "?"),
                    "title": v.get("task_title", ""),
                    "passed": bool(v.get("passed")),
                    "n_criteria": len(items),
                    "tier1_passed": t1.get("passed") if t1 else None,
                }
            )
    return out


def load_verdict(workdir: str, date: str, h: str) -> dict | None:
    if not _DATE_RE.match(date) or not _HASH_RE.match(h):
        return None
    vpath = os.path.join(_verdict_dir(workdir), date, h, "verdict.json")
    if not os.path.isfile(vpath):
        return None
    try:
        return json.load(open(vpath))
    except Exception:
        return None


def load_jsonl(workdir: str, name: str, limit: int = 2000) -> list:
    path = board_file_path(workdir, name)
    rows = []
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


def load_ticks(project: str, limit: int = 500) -> list[dict]:
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


def stats(verdicts: list[dict]) -> dict:
    n_pass = sum(1 for v in verdicts if v["passed"])
    return {
        "total": len(verdicts),
        "passed": n_pass,
        "failed": len(verdicts) - n_pass,
        "pass_rate": round(100 * n_pass / len(verdicts)) if verdicts else 0,
    }


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
@media(min-width:600px){.grid{grid-template-columns:repeat(4,1fr)}body{padding:20px}}
.stat{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;padding:10px;text-align:center}
.stat .n{font-size:clamp(19px,5vw,26px);font-weight:800;color:#4ade80;display:block}
.stat .l{font-size:10px;color:#8a8aa3;text-transform:uppercase;letter-spacing:.5px}
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
<footer>gitreins serve · live reads from .gitreins/history + board + scheduler ledger</footer>
<script>
const esc=s=>{const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML};
const badge=v=>v?'<span class="badge pass">PASS</span>':'<span class="badge fail">FAIL</span>';
let V=[],filter='all',q='';
async function j(url){const r=await fetch(url);if(!r.ok)throw new Error(url);return r.json()}
async function boot(){
  const [st,vs,ev,tk]=await Promise.all([j('/api/stats'),j('/api/verdicts'),j('/api/events'),j('/api/ticks')]);
  V=vs.verdicts;
  document.getElementById('sub').textContent=st.repo+' · live view · '+st.generated;
  document.getElementById('stats').innerHTML=
    '<div class="stat"><span class="n">'+st.total+'</span><span class="l">Judgments</span></div>'+
    '<div class="stat"><span class="n">'+st.passed+'</span><span class="l">Passed</span></div>'+
    '<div class="stat"><span class="n">'+st.failed+'</span><span class="l">Failed (gates held)</span></div>'+
    '<div class="stat"><span class="n">'+st.pass_rate+'%</span><span class="l">Pass rate</span></div>';
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
  }).join('')||'<p style="color:#5a5a75;font-size:12px">no scheduler ledger on this host</p>';
}
function render(){
  const rows=V.filter(v=>(filter==='all'||(filter==='pass')===v.passed)&&(!q||(v.task_id+' '+v.title).toLowerCase().includes(q)));
  document.getElementById('list').innerHTML=rows.map(v=>'<div class="row" onclick="show(\\''+v.date+'\\',\\''+v.hash+'\\')">'+
    '<div class="top"><span class="task">'+esc(v.task_id)+'</span><span class="title">'+esc(v.title)+'</span>'+badge(v.passed)+'</div>'+
    '<div class="meta">'+v.date+' · '+v.hash+' · '+v.n_criteria+' criteria · tier1: '+(v.tier1_passed==null?'—':(v.tier1_passed?'PASS':'FAIL'))+'</div></div>').join('')
    ||'<p style="color:#5a5a75;font-size:13px">no judgments match</p>';
}
async function show(date,hash){
  const r=await fetch('/api/verdicts/'+date+'/'+hash);if(!r.ok)return;
  const v=await r.json();
  const stages=v.stages||{};const t1=stages.tier1||{};const t2=stages.tier2||{};
  const items=v.items||(t2.items||[]);
  const d=document.getElementById('detail');
  d.innerHTML='<button class="close" onclick="document.getElementById(\\'detail\\').style.display=\\'none\\'">✕ close</button>'+
   '<h3>'+esc(v.task_id||'?')+' — '+esc(v.task_title||'')+'</h3>'+
   '<div style="color:#8a8aa3;font-size:11px">'+date+' · '+hash+' · overall '+(v.passed?'PASS':'FAIL')+'</div>'+
   '<div class="sec">Criteria ('+items.length+')</div>'+
   items.map(it=>'<div class="crit '+(it.status=='PASS'?'p':'f')+'"><div class="c">'+(it.status=='PASS'?'✅':'❌')+' '+esc(it.criterion)+'</div><div class="d">'+esc(it.detail)+'</div></div>').join('')
   ||'<p style="color:#5a5a75;font-size:12px">no per-criterion items recorded</p>'+
   ((t1.summary)?'<div class="sec">Tier 1 — static gates</div><pre>'+esc(t1.summary)+'</pre>':'')+
   ((t2.summary)?'<div class="sec">Tier 2 — judge summary</div><pre>'+esc(t2.summary)+'</pre>':'')+
   (v.summary?'<div class="sec">Verdict summary</div><pre>'+esc(v.summary)+'</pre>':'');
  d.style.display='block';d.scrollIntoView({behavior:'smooth',block:'start'});
}
document.querySelectorAll('.tab[data-f]').forEach(t=>t.onclick=()=>{document.querySelectorAll('.tab[data-f]').forEach(x=>x.classList.remove('active'));t.classList.add('active');filter=t.dataset.f;render()});
document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase();render()};
boot();
</script></body></html>"""


# ── HTTP server ──────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    workdir = "."
    project = ""

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self):  # noqa: N802 (http.server API)
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/stats":
                vs = list_verdicts(self.workdir)
                from datetime import datetime, timezone

                self._json(
                    {
                        **stats(vs),
                        "repo": os.path.basename(os.path.abspath(self.workdir)),
                        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    }
                )
            elif path == "/api/verdicts":
                self._json({"verdicts": list_verdicts(self.workdir)})
            elif path.startswith("/api/verdicts/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) != 4:
                    return self._json({"error": "use /api/verdicts/<date>/<hash>"}, 400)
                v = load_verdict(self.workdir, parts[2], parts[3])
                if v is None:
                    return self._json({"error": "not found"}, 404)
                self._json(v)
            elif path == "/api/tasks":
                self._json({"tasks": load_jsonl(self.workdir, "tasks.jsonl")})
            elif path == "/api/events":
                self._json({"events": load_jsonl(self.workdir, "events.jsonl")})
            elif path == "/api/ticks":
                self._json({"ticks": load_ticks(self.project) if self.project else []})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # keep the server alive on handler bugs
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("GITREINS_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


def serve(
    workdir: str,
    host: str = "127.0.0.1",
    port: int = 8616,
    project: str = "",
    open_browser: bool = False,
) -> None:
    Handler.workdir = workdir
    Handler.project = project
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"GitReins judgment browser: {url}")
    print(f"repo: {os.path.abspath(workdir)}  (Ctrl-C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
