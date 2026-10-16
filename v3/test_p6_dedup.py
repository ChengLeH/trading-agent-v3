#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression test for BUG-1 (2026-10-17, SKHY run):
collect_phase6_revision() must not silently last-wins on duplicate claim_ids.
A trailing dispute stub must not swallow a real revision.

Run: PYTHONPATH=. .venv/bin/python -m v3.test_p6_dedup
"""
from v3.runner import PipelineRunner
from v3.pipeline import RunState
from v3 import reporting

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail else ""))


def make_runner(claims):
    r = PipelineRunner()
    r.run_id = "t"
    r.orch.runs["t"] = RunState(run_id="t", ticker="T",
                                claims=[dict(c) for c in claims])
    r._phase6_revision_history = [{"revisions": []}]
    # stub out the heavy tail (render + validate + next prompt)
    r.orch.snapshot = lambda rid: {}
    reporting.render_report = lambda snap: {"report_markdown": "", "sidecar": {}}
    r.orch.validate_final = lambda rid: ([], False, None)
    r.prepare_phase6 = lambda: ("reviewer", "prompt")
    return r


OLD = {"claim_id": "soc.3", "text": "OLD TEXT", "author_role": "social_analyst"}
NEW = {"claim_id": "soc.3", "text": "NEW TEXT", "author_role": "social_analyst"}
STUB = {"claim_id": "soc.3", "dispute": True, "dispute_reason": "not mine"}

# 1. SKHY repro: content first, dispute stub after -> stub must NOT win
r = make_runner([OLD])
ret = r.collect_phase6_revision([dict(NEW), dict(STUB)])
c = r.orch.runs["t"].claims[0]
check("skhy-repro: revised text applied", c["text"] == "NEW TEXT", c["text"][:20])
check("skhy-repro: dispute stub did not pollute",
      c.get("dispute") is not True, f"dispute={c.get('dispute')}")
check("skhy-repro: warning recorded", len(ret["dedup_warnings"]) == 1,
      str(ret["dedup_warnings"]))
check("skhy-repro: history uses deduped list",
      len(r._phase6_revision_history[-1]["revisions"]) == 1)

# 2. reverse order: stub first, content after -> content wins
r = make_runner([OLD])
ret = r.collect_phase6_revision([dict(STUB), dict(NEW)])
c = r.orch.runs["t"].claims[0]
check("reverse-order: revised text applied", c["text"] == "NEW TEXT")
check("reverse-order: warning recorded", len(ret["dedup_warnings"]) == 1)

# 3. two genuinely different texts -> explicit error, no silent choice
r = make_runner([OLD])
try:
    r.collect_phase6_revision([dict(NEW),
                               {"claim_id": "soc.3", "text": "OTHER TEXT"}])
    check("conflict: raises ValueError", False, "no error raised")
except ValueError as e:
    check("conflict: raises ValueError", "soc.3" in str(e), str(e)[:60])

# 4. identical content twice -> keep first, warn, no error
r = make_runner([OLD])
ret = r.collect_phase6_revision([dict(NEW), dict(NEW)])
check("identical-dup: applied once", ret["applied_revisions"] == 1)
check("identical-dup: warning recorded", len(ret["dedup_warnings"]) == 1)

# 5. no duplicates -> unchanged behavior
r = make_runner([OLD])
ret = r.collect_phase6_revision([dict(NEW)])
check("normal: applied", ret["applied_revisions"] == 1)
check("normal: no warnings", ret["dedup_warnings"] == [])

print(f"\n{len(passed)} passed, {len(failed)} failed")
raise SystemExit(1 if failed else 0)
