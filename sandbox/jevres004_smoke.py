"""Smoke check for the JEVRES-004 wiring (run from the worktree root)."""
from engine.prescreen import (
    build_prescreen_question,
    criterion_citation_paths,
    run_prescreen,
    assemble_prescreen_task,
    attach_prescreen,
    PrescreenResult,
)
from engine.resolution import ResolutionVerdict
from engine.evaluator import Verdict, VerdictItem
from engine.persist import build_verdict_data

# citation extraction
paths = criterion_citation_paths("Wired in engine/evaluator.py:1180 and tested in tests/test_x.py")
print("citations:", paths)
print("no-cite:", criterion_citation_paths("no paths here"))
print("excluded:", criterion_citation_paths("see .env and engine/x.py"))

# question building
q = build_prescreen_question(["Criterion A with engine/a.py:12", "Criterion B" + " x" * 500])
print("question head:", q.splitlines()[0][:60])
print("question n lines:", len(q.splitlines()))

# from_verdict spread
v = ResolutionVerdict(
    question=q, verdict="REVIEW", probability=0.70,
    missing_kind="test", evidence_quality=2, evidence_quality_score=2.3,
)
ps = PrescreenResult.from_verdict(v, ["Crit A engine/a.py:12", "Crit B"])
print("probs:", [r.probability for r in ps.criteria])
print("kinds:", [r.missing_kind for r in ps.criteria])
print("quality:", ps.evidence_quality)
print("row dict:", ps.criteria[0].to_dict())

# prompt block
block = assemble_prescreen_task(ps)
print("block has INPUT ONLY:", "INPUT ONLY" in block)
print("block first line:", block.splitlines()[0])

# attribution through attach_prescreen
verdict = Verdict(verdict="COMPLETE", items=[
    VerdictItem(criterion="Crit A engine/a.py:12", status="PASS", detail="ok tests/test_a.py:9"),
    VerdictItem(criterion="Crit B", status="FAIL", detail="not wired"),
])
attributed = attach_prescreen(verdict, ps)
print("prob on item:", attributed.items[0].resolution_probability,
      "cited:", attributed.items[0].cited_path)
print("detail decorated:", attributed.items[0].detail)
print("fail untouched:", attributed.items[1].detail)
print("prescreen attached:", attributed.prescreen is not None)

# degraded: no prescreen → verdict unchanged identity
same = attach_prescreen(verdict, None)
print("unchanged when None:", same is verdict)

# persistence shape
class _Task:
    id = "T-1"
    title = "t"
    criteria = ["a"]

class _Result:
    verdict = attributed
    summary = "s"
    pipeline_result = {}
    passed = True

data = build_verdict_data(".", _Task(), _Result())
print("persisted item keys:", sorted(data["items"][0].keys()))
print("prescreen key present:", "prescreen" in data)

# degraded persistence: no attribution keys on a plain verdict
class _ResultPlain:
    verdict = Verdict(verdict="COMPLETE", items=[VerdictItem(criterion="a", status="PASS", detail="d")])
    summary = "s"
    pipeline_result = {}
    passed = True
data_plain = build_verdict_data(".", _Task(), _ResultPlain())
print("plain item keys:", sorted(data_plain["items"][0].keys()))
print("plain has prescreen:", "prescreen" in data_plain)

# no-criteria abstain
res = run_prescreen({"criteria": []})
print("no-criteria abstained:", res.abstained, res.abstain_reason)
