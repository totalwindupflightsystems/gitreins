#!/usr/bin/env python3
"""GitReins Judgment Viewer — static site generator.

Scans a repo's judgment stores and emits ONE self-contained dark HTML page
(a "git tree browser, but for judgments"):

  <repo>/.gitreins/history/<date>/<hash>/verdict.json   -> per-judgment detail
  <repo>/.coding-hermes/board/events.jsonl              -> fleet event timeline
  <repo>/.coding-hermes/board/tasks.jsonl               -> task titles/status
  ~/.hermes/coding-hermes/scheduler.db                  -> tick outcomes/cost

Usage:
  python3 judgment_viewer.py --repo /home/kara/gitreins-poc --out /home/kara/gitreins-judgments.html
"""

import argparse
import html
import json
import os
import sqlite3
import sys

try:
    from engine.repo_paths import board_file_path
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from engine.repo_paths import board_file_path

TICKS_DB = os.path.expanduser("~/.hermes/coding-hermes/scheduler.db")


def load_verdicts(repo):
    out = []
    hist = os.path.join(repo, ".gitreins", "history")
    if not os.path.isdir(hist):
        return out
    for day in sorted(os.listdir(hist)):
        ddir = os.path.join(hist, day)
        if not os.path.isdir(ddir):
            continue
        for h in sorted(os.listdir(ddir)):
            vpath = os.path.join(ddir, h, "verdict.json")
            if not os.path.isfile(vpath):
                continue
            try:
                v = json.load(open(vpath))
            except Exception:
                continue
            stages = v.get("stages") or {}
            t1 = stages.get("tier1") or {}
            t2 = stages.get("tier2") or {}
            items = v.get("items") or (t2.get("items") or [])
            out.append(
                {
                    "date": day,
                    "hash": h,
                    "task_id": v.get("task_id", "?"),
                    "title": v.get("task_title", ""),
                    "criteria": v.get("task_criteria", []),
                    "passed": bool(v.get("passed")),
                    "items": [
                        {
                            "criterion": i.get("criterion", "?"),
                            "status": i.get("status", "?"),
                            "detail": (i.get("detail") or "")[:1500],
                        }
                        for i in items
                    ],
                    "tier1_passed": t1.get("passed") if t1 else None,
                    "tier1_summary": (t1.get("summary") or "")[:4000],
                    "tier2_summary": (t2.get("summary") or "")[:4000],
                    "summary": (v.get("summary") or "")[:1500],
                }
            )
    return out


def load_events(repo):
    path = board_file_path(repo, "events.jsonl")
    evs = []
    if os.path.isfile(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            detail = {}
            try:
                detail = json.loads(e.get("detail") or "{}")
            except Exception:
                pass
            evs.append(
                {
                    "id": e.get("id"),
                    "ts": e.get("timestamp", ""),
                    "type": e.get("event_type", "?"),
                    "task": e.get("task_id") or "",
                    "actor": e.get("actor") or "",
                    "commit": (detail.get("commit") or "")[:8],
                    "tick": detail.get("tick"),
                    "verdict": detail.get("verdict") or "",
                }
            )
    return evs


def load_tasks(repo):
    path = board_file_path(repo, "tasks.jsonl")
    tasks = {}
    if os.path.isfile(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            tasks[t.get("id")] = {
                "title": t.get("title", ""),
                "status": t.get("status", "?"),
                "priority": t.get("priority", ""),
            }
    return tasks


def load_ticks(name="gitreins-poc"):
    if not os.path.exists(TICKS_DB):
        return []
    try:
        db = sqlite3.connect(TICKS_DB)
        rows = db.execute(
            "SELECT id, spawned_at, status, outcome, commits, files_changed, "
            "round(cost_usd,2), substr(coalesce(error,''),1,120) "
            "FROM ticks WHERE project_name=? ORDER BY spawned_at DESC LIMIT 500",
            (name,),
        ).fetchall()
        db.close()
    except Exception:
        return []
    keys = ["id", "spawned_at", "status", "outcome", "commits", "files", "cost", "error"]
    return [dict(zip(keys, r)) for r in rows]


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitReins — Judgment Browser</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:#0f0f1a;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:12px;line-height:1.5}
h1{font-size:clamp(22px,6vw,36px);background:linear-gradient(135deg,#f7971e,#ffd200);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:2px}
.sub{color:#8a8aa3;font-size:13px;margin-bottom:14px}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media(min-width:600px){.stats{grid-template-columns:repeat(4,1fr)}body{padding:20px}}
.stat{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;padding:10px;text-align:center}
.stat .n{font-size:clamp(19px,5vw,26px);font-weight:800;color:#4ade80;display:block}
.stat .l{font-size:10px;color:#8a8aa3;text-transform:uppercase;letter-spacing:.5px}
.tabs{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.tab{background:#12121f;border:1px solid #2a2a3e;border-radius:20px;padding:5px 14px;font-size:12px;cursor:pointer;color:#8a8aa3}
.tab.active{background:#166534;color:#4ade80;border-color:#166534}
.tab input{background:#12121f;border:1px solid #2a2a3e;border-radius:16px;color:#e0e0e0;padding:5px 12px;font-size:12px;width:150px;outline:none}
.row{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;padding:10px 12px;margin-bottom:7px;cursor:pointer}
.row:hover{border-color:#4a4a6a}
.row .top{display:flex;justify-content:space-between;gap:8px;align-items:baseline;flex-wrap:wrap}
.row .task{font-weight:700;color:#60a5fa;font-size:13px}
.row .title{color:#c4c4d8;font-size:12px;flex:1;min-width:120px}
.badge{padding:2px 9px;border-radius:20px;font-size:10px;font-weight:800;white-space:nowrap}
.pass{background:#166534;color:#4ade80}.fail{background:#3b0820;color:#f472b6}
.warn{background:#3b1e08;color:#f59e0b}.mute{background:#12121f;color:#8a8aa3}
.row .meta{color:#5a5a75;font-size:10.5px;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
#detail{display:none;background:#12121f;border:1px solid #4a4a6a;border-radius:12px;padding:14px;margin:10px 0}
#detail h3{color:#ffd200;font-size:15px;margin-bottom:6px}
#detail .crit{margin:8px 0;padding:8px 10px;background:#1a1a2e;border-radius:8px;border-left:3px solid #2a2a3e}
#detail .crit.p{border-left-color:#4ade80}#detail .crit.f{border-left-color:#f472b6}
#detail .crit .c{font-size:12.5px;font-weight:600;color:#e0e0e0}
#detail .crit .d{font-size:11.5px;color:#9a9ab3;margin-top:4px;white-space:pre-wrap}
#detail pre{background:#0a0a14;border:1px solid #2a2a3e;border-radius:8px;padding:10px;font-size:10.5px;overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0;white-space:pre-wrap}
#detail .sec{color:#60a5fa;font-size:12px;text-transform:uppercase;letter-spacing:.5px;margin:12px 0 4px}
.close{float:right;background:#2a2a3e;color:#e0e0e0;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
.ev{border-bottom:1px solid #1f1f33;padding:6px 2px;font-size:11.5px;display:flex;gap:8px;flex-wrap:wrap}
.ev .ts{color:#5a5a75;font-family:ui-monospace,Menlo,monospace;white-space:nowrap}
.ev .t{color:#a78bfa;min-width:90px}
.ev .k{color:#60a5fa}
.ev .v{color:#c4c4d8}
h2{font-size:16px;color:#ffd200;margin:18px 0 8px}
.footer{text-align:center;color:#5a5a75;font-size:10.5px;padding:14px 0 6px}
</style></head><body>
<h1>⚖️ Judgment Browser</h1>
<div class="sub">__SUBTITLE__</div>
<div class="stats">
<div class="stat"><span class="n">__N_VERDICTS__</span><span class="l">Judgments</span></div>
<div class="stat"><span class="n">__N_PASS__</span><span class="l">Passed</span></div>
<div class="stat"><span class="n">__N_FAIL__</span><span class="l">Failed (gates held)</span></div>
<div class="stat"><span class="n">__RATE__%</span><span class="l">Pass rate</span></div>
</div>
<div class="tabs">
<div class="tab active" data-f="all">All</div>
<div class="tab" data-f="pass">PASS</div>
<div class="tab" data-f="fail">FAIL</div>
<div class="tab"><input id="q" placeholder="search task…"></div>
</div>
<div id="list"></div>
<div id="detail"></div>
<h2>🕰️ Board Event Timeline <span style="font-size:11px;color:#5a5a75">(__N_EVENTS__ events)</span></h2>
<div style="background:#1a1a2e;border:1px solid #2a2a3e;border-radius:12px;padding:10px 12px;max-height:420px;overflow-y:auto" id="evlist"></div>
<h2>🖥️ Scheduler Ticks <span style="font-size:11px;color:#5a5a75">(latest 500)</span></h2>
<div style="background:#1a1a2e;border:1px solid #2a2a3e;border-radius:12px;padding:10px 12px;max-height:340px;overflow-y:auto" id="ticklist"></div>
<div class="footer">Generated from .gitreins/history + board + scheduler ledger · static page, no server needed</div>
<script>
const D = __DATA__;
const EV = __EVENTS__;
const TK = __TICKS__;
let filter='all', q='';
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
function badge(v){return v?'<span class="badge pass">PASS</span>':'<span class="badge fail">FAIL</span>'}
function render(){
  const list=document.getElementById('list');
  const rows=D.filter(v=>(filter==='all'||(filter==='pass')===v.passed)&&(!q||(v.task_id+' '+v.title).toLowerCase().includes(q)));
  list.innerHTML=rows.map((v,i)=>`<div class="row" onclick="show('${v.date}','${v.hash}')">
    <div class="top"><span class="task">${esc(v.task_id)}</span><span class="title">${esc(v.title||'')}</span>${badge(v.passed)}</div>
    <div class="meta">${v.date} · ${v.hash} · ${v.items.length} criteria · tier1: ${v.tier1_passed==null?'—':(v.tier1_passed?'PASS':'FAIL')}</div>
  </div>`).join('')||'<p style="color:#5a5a75;font-size:13px">no judgments match</p>';
  document.getElementById('evlist').innerHTML=EV.slice().reverse().map(e=>`<div class="ev"><span class="ts">${esc((e.ts||'').slice(0,16))}</span><span class="t">${esc(e.type)}</span><span class="k">${esc(e.task)}</span><span class="v">${e.commit?'<span class="mono">'+esc(e.commit)+'</span> ':''}${esc(e.verdict)}${e.tick?' · tick '+esc(e.tick):''}</span></div>`).join('');
  document.getElementById('ticklist').innerHTML=TK.map(t=>`<div class="ev"><span class="ts">${esc((t.spawned_at||'').slice(0,16))}</span><span class="t">${t.status=='completed'&&t.outcome=='committed'?'🟢 committed':(t.status=='completed'?'🟡 '+esc(t.outcome||t.status):'🔴 '+esc(t.status))}</span><span class="v">${t.commits||0} commits · ${t.files||0} files · $${esc(t.cost==null?'0':t.cost)}${t.error?' · '+esc(t.error):''}</span></div>`).join('');
}
function show(date,hash){
  const v=D.find(x=>x.date===date&&x.hash===hash);if(!v)return;
  const d=document.getElementById('detail');
  d.innerHTML=`<button class="close" onclick="document.getElementById('detail').style.display='none'">✕ close</button>
  <h3>${esc(v.task_id)} — ${esc(v.title||'')}</h3>
  <div style="color:#8a8aa3;font-size:11px">${v.date} · ${v.hash} · overall ${v.passed?'PASS':'FAIL'}</div>
  <div class="sec">Criteria (${v.items.length})</div>
  ${v.items.map(it=>`<div class="crit ${it.status=='PASS'?'p':'f'}"><div class="c">${it.status=='PASS'?'✅':'❌'} ${esc(it.criterion)}</div><div class="d">${esc(it.detail)}</div></div>`).join('')||'<p style="color:#5a5a75;font-size:12px">no per-criterion items recorded</p>'}
  ${v.tier1_summary?`<div class="sec">Tier 1 — static gates</div><pre>${esc(v.tier1_summary)}</pre>`:''}
  ${v.tier2_summary?`<div class="sec">Tier 2 — judge summary</div><pre>${esc(v.tier2_summary)}</pre>`:''}
  ${v.summary?`<div class="sec">Verdict summary</div><pre>${esc(v.summary)}</pre>`:''}`;
  d.style.display='block';
  d.scrollIntoView({behavior:'smooth',block:'start'});
}
document.querySelectorAll('.tab[data-f]').forEach(t=>t.onclick=()=>{document.querySelectorAll('.tab[data-f]').forEach(x=>x.classList.remove('active'));t.classList.add('active');filter=t.dataset.f;render()});
document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase();render()};
render();
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project", default="gitreins-poc", help="scheduler project name for ticks")
    args = ap.parse_args()

    verdicts = load_verdicts(args.repo)
    events = load_events(args.repo)
    ticks = load_ticks(args.project)
    n_pass = sum(1 for v in verdicts if v["passed"])
    n_fail = len(verdicts) - n_pass
    rate = round(100 * n_pass / len(verdicts)) if verdicts else 0
    page = (
        TEMPLATE.replace(
            "__SUBTITLE__",
            html.escape(
                f"{os.path.basename(args.repo)} · every LLM judgment the harness ever made, with the evidence"
            ),
        )
        .replace("__N_VERDICTS__", str(len(verdicts)))
        .replace("__N_PASS__", str(n_pass))
        .replace("__N_FAIL__", str(n_fail))
        .replace("__RATE__", str(rate))
        .replace("__N_EVENTS__", str(len(events)))
        .replace("__DATA__", json.dumps(verdicts))
        .replace("__EVENTS__", json.dumps(events))
        .replace("__TICKS__", json.dumps(ticks))
    )
    with open(args.out, "w") as f:
        f.write(page)
    print(
        f"{len(verdicts)} verdicts ({n_pass} pass / {n_fail} fail, {rate}%), "
        f"{len(events)} events, {len(ticks)} ticks -> {args.out} ({os.path.getsize(args.out) // 1024} KB)"
    )


if __name__ == "__main__":
    main()
