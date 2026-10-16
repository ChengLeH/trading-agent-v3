"""Real V4 object/coordinator chain with deterministic cloud test doubles.

No tests load or call a model. Test doubles record which requests would be
sent; this verifies orchestration, not the quality of LLM judgments.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from v3.pipeline import RunState
from v3.facts import Fact
from .runner import PipelineRunnerV4, ROLES
from .schema import Claim
from .test_contracts import claim


class CloudDouble:
    execution = "cloud"

    def __init__(self, identity="test/atomic", responder=None):
        self.identity = identity
        self.calls = []
        self.responder = responder

    def complete(self, prompt):
        self.calls.append(prompt)
        if self.responder:
            return self.responder(prompt)
        # Parsing test protocol JSON is not production semantic routing.
        payload = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
        return verdict(payload["claim_id"])


def verdict(cid, status="PASS"):
    return dict(claim_id=cid, verdict=status,
                findings=[] if status == "PASS" else [dict(issue="來源與斷言不一致", evidence="test", suggested_fix="局部修正")])


def seeded():
    r = PipelineRunnerV4()
    r.run_id = "fixture"
    r.orch.runs[r.run_id] = RunState(run_id=r.run_id, ticker="TEST",
                                    manifest={"subject": {"ticker": "TEST"}, "as_of": "2026-10-16"})
    r.run.events = {}
    r.orch.submit_batch(r.run_id, {"bull": [claim("a"), claim("b", ["a"]), claim("c", ["b"]), claim("independent")]})
    return r


def review_all(r):
    prepared = r.prepare_phase6_atomic()
    return r.collect_phase6_atomic({cid: verdict(cid) for cid, _ in prepared["atomic_prompts"]}, model_identity="test/first")


class Integration(unittest.TestCase):
    @patch("v3.pipeline.build_packet", side_effect=AssertionError("legacy packet"))
    @patch("v3.validation.ValidationService.validate_stage", side_effect=AssertionError("legacy validation"))
    @patch("v3.runner.build_subagent_prompt", side_effect=AssertionError("legacy schema"))
    def test_full_role_ingestion_to_final_report_and_reload(self, *_):
        from .checkpoint import save_checkpoint, load_checkpoint
        def author_response(prompt):
            packet = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
            role = packet["role"]
            c = claim(role + ".1", [x["claim_id"] for x in packet["claims"]])
            c["author_role"] = role
            output = {"claims": [c]}
            if role == "trader":
                output["trade"] = {"direction": "WAIT"}
                c["assertion"] = dict(kind="trade_plan", trade=output["trade"])
            return output
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "news.json"
            path.write_text(json.dumps({"items": [dict(title="來源測試", url="https://example.test/source", published="2026-10-15")]}))
            manifest = dict(subject={"ticker": "TEST"}, as_of="2026-10-16",
                            sources=[dict(id="news", type="news_json", path=str(path))])
            r = PipelineRunnerV4()
            author = CloudDouble("test/author", author_response)
            atomic, deep = CloudDouble(), CloudDouble("test/deep")
            sweep = CloudDouble("test/global", lambda _: [])
            result = r.execute("TEST", manifest, author, atomic, deep, sweep)
            self.assertFalse(result["blocked"], result)
            self.assertEqual(len(author.calls), len(ROLES))
            self.assertEqual(len(atomic.calls), len(ROLES))
            self.assertEqual(len(result["sidecar"]["evidence"]), 1)
            self.assertEqual(result["sidecar"]["total_claims"], len(ROLES))
            checkpoint = Path(temp) / "checkpoint.json"
            save_checkpoint(r, checkpoint)
            restored = load_checkpoint(checkpoint)
            self.assertEqual(restored.run.claims, r.run.claims)
            self.assertEqual(restored.run.analyses, r.run.analyses)
            with self.assertRaises(ValueError):
                restored.finalize()
            again = restored.execute_phase6(atomic, deep, author, sweep)
            self.assertFalse(again["blocked"], again)
            self.assertEqual(again["report_markdown"], result["report_markdown"])

    def test_executor_patch_round_only_rechecks_affected_descendants(self):
        r = seeded()
        def atomic_response(prompt):
            task = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
            status = "FAIL" if task["claim_id"] == "a" and task["claim"]["text"] != "修正版" else "PASS"
            return verdict(task["claim_id"], status)
        atomic = CloudDouble("test/atomic", atomic_response)
        deep = CloudDouble("test/deep", lambda _: verdict("a", "FAIL"))
        author = CloudDouble("test/author", lambda _: [dict(claim_id="a", text="修正版")])
        sweep = CloudDouble("test/global", lambda _: [])
        result = r.execute_phase6(atomic, deep, author, sweep)
        self.assertFalse(result["blocked"], result)
        self.assertEqual(len(atomic.calls), 7)
        self.assertEqual(len(deep.calls), 1)
        self.assertEqual(len(sweep.calls), 1)
        self.assertEqual(result["sidecar"]["revision_history"][0]["affected_claim_ids"], ["a", "b", "c"])

    def test_global_finding_uses_semantic_linker_then_requires_new_sweep(self):
        r = seeded()
        responses = iter([[dict(issue="跨章錯誤", claim_ids=[], unresolved=True)], []])
        sweep = CloudDouble("test/global", lambda _: next(responses))
        linker = CloudDouble("test/linker", lambda _: [dict(finding_index=0, claim_ids=["a"], unresolved=False)])
        patcher = CloudDouble("test/author", lambda _: [dict(claim_id="a", text="修正版")])
        result = r.execute_phase6(CloudDouble(), CloudDouble("test/deep"), patcher, sweep, linker)
        self.assertFalse(result["blocked"], result)
        self.assertEqual(len(linker.calls), 1)
        self.assertEqual(len(sweep.calls), 2)

    def test_cloud_failure_and_revision_exhaustion_never_return_report(self):
        r = seeded()
        def failure(_):
            raise TimeoutError("cloud unavailable")
        result = r.execute_phase6(CloudDouble(responder=failure), CloudDouble("test/deep"), CloudDouble(), CloudDouble())
        self.assertTrue(result["blocked"])
        self.assertNotIn("report_markdown", result)
        r = seeded()
        r.max_revisions = 0
        sweep = CloudDouble("test/global", lambda _: [dict(issue="contradiction", claim_ids=["a"])])
        result = r.execute_phase6(CloudDouble(), CloudDouble("test/deep"), CloudDouble(), sweep)
        self.assertTrue(result["blocked"])
        self.assertNotIn("report_markdown", result)

    def test_structured_event_cap_and_claim_binding_without_prose(self):
        r = seeded()
        f = Fact("fact.event", 100, "SHARES", "2026", concept="Release",
                 entity="TEST", source_id="fixture")
        r.run.fact_store.add(f)
        event = dict(event_id="event.cap", date="2026-11-01", source_ref=f.fact_id,
                     entity="TEST", concept="Release", period="2026", unit="SHARES",
                     scope="combined", basis="reported", is_aggregate=True, value=100)
        r.orch.register_events(r.run_id, [event])
        c = claim("event.claim")
        c.update(author_role="bear", text="完全無關的文字仍由結構檢查", refs=[event["event_id"]],
                 assertion=dict(kind="event", event_ref=event["event_id"], is_aggregate=False),
                 bindings=[{k: event[k] for k in ("entity", "concept", "period", "unit", "scope", "basis")}])
        c["bindings"][0]["ref_id"] = event["event_id"]
        r.orch.submit_batch(r.run_id, {"bear": [c]})
        findings, blocked = r.orch.validate_stage(r.run_id, "final")
        self.assertTrue(blocked)
        self.assertTrue(any(f.get("check_id") == "event_binding" for f in findings))
        member = {**event, "event_id": "event.member", "is_aggregate": False, "value": 101, "aggregate_ref": "event.cap"}
        r.orch.register_events(r.run_id, [event, member])
        findings, _ = r.orch.validate_stage(r.run_id, "final")
        self.assertTrue(any(f.get("check_id") == "event_total" for f in findings))

    def test_typed_calculation_dag_and_mismatch(self):
        from .pipeline import calculation_findings
        r = seeded()
        for fid, value in (("start", 10), ("end", 20)):
            r.run.fact_store.add(Fact(fid, value, "USD", "2026", concept="Amount", source_id="fixture"))
        r.run.calc_store.add("first", 1.0, "growth", ["end", "start"])
        r.run.calc_store.add("second", 19.0, "fcf", ["end", "first"])
        self.assertFalse(any(f["status"] == "FAIL" for f in calculation_findings(r.run)))
        r.run.calc_store._calcs["first"]["value"] = 2
        self.assertTrue(any(f["status"] == "FAIL" for f in calculation_findings(r.run)))

    def test_trade_claim_patch_regenerates_numeric_resources_and_risk_context(self):
        r = seeded()
        original = dict(direction="BUY", entry=100, stop=90, target=120, position_pct=10)
        c = claim("trader.plan")
        c.update(author_role="trader", dependencies=["a"], assertion=dict(kind="trade_plan", trade=original))
        r.orch.register_trade(r.run_id, original)
        r.orch.submit_batch(r.run_id, {"trader": [c]})
        risk = claim("risk.decision", ["trader.plan"])
        risk["author_role"] = "risk_manager"
        r.orch.submit_batch(r.run_id, {"risk_manager": [risk]})
        review_all(r)
        r.prepare_phase6_revision_v4([dict(claim_id="trader.plan", issue="change plan")])
        updated = {**original, "stop": 95}
        pending = r.collect_phase6_revision([dict(claim_id="trader.plan", assertion=dict(kind="trade_plan", trade=updated))])
        self.assertEqual(r.run.trade, updated)
        self.assertEqual(r.run.fact_store.get("fact.Trade.Stop").value, 95)
        self.assertEqual(r.run.calc_store.get("calc.trade.risk_reward")["value"], 4)
        self.assertEqual(set(pending["revalidate_claim_ids"]), {"trader.plan", "risk.decision"})
        self.assertEqual(r.orch.get_packet(r.run_id, "risk_manager")["trade"], updated)
        self.assertFalse(any(f["status"] == "FAIL" for f in r.orch.validate_stage(r.run_id, "final")[0]))

    def test_no_old_prose_gate_or_renderer_is_reachable(self):
        import v3.validation as legacy
        import v3.reporting as old_report
        with patch.object(legacy, "check_event_timeline_consistency", side_effect=AssertionError("prose")), \
             patch.object(old_report, "render_report", side_effect=AssertionError("old renderer")):
            r = seeded()
            first, deep = CloudDouble(), CloudDouble("test/deep")
            sweep = CloudDouble("test/global", lambda _: [])
            result = r.execute_phase6(first, deep, sweep, sweep)
            self.assertFalse(result["blocked"])
            self.assertEqual(len(first.calls), 4)
            self.assertEqual(deep.calls, [])
            self.assertEqual(len(sweep.calls), 1)

    def test_patch_invalidates_descendants_and_syncs_every_view(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep([])
        r.prepare_phase6_revision_v4([dict(claim_id="a", issue="fix")])
        result = r.collect_phase6_revision([dict(claim_id="a", text="修正版")])
        self.assertEqual(result["revalidate_claim_ids"], ["a", "b", "c"])
        self.assertEqual({cid for cid, _ in result["atomic_prompts"]}, {"a", "b", "c"})
        self.assertEqual(set(r.receipts), {"independent"})
        self.assertEqual(r.run.analyses["bull"]["claims"][0]["text"], "修正版")
        self.assertEqual(r.orch.get_packet(r.run_id, "trader")["claims"][0]["text"], "修正版")
        with self.assertRaises(ValueError):
            r.finalize()
        r.collect_phase6_atomic({cid: verdict(cid) for cid, _ in result["atomic_prompts"]})
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep([])
        self.assertIn("修正版", r.finalize()["report_markdown"])

    def test_patch_rolls_back_on_duplicate_unknown_author_or_cycle(self):
        for patches in ([dict(claim_id="a", dependencies=["c"])],
                        [dict(claim_id="ghost", text="x")],
                        [dict(claim_id="a", author_role="bear")],
                        [dict(claim_id="a", text="x"), dict(claim_id="a", text="y")]):
            r = seeded()
            review_all(r)
            before = r.orch.snapshot(r.run_id)
            r.prepare_phase6_revision_v4([dict(claim_id="a", issue="fix")])
            with self.assertRaises(ValueError):
                r.collect_phase6_revision(patches)
            self.assertEqual(r.orch.snapshot(r.run_id), before)

    def test_missing_wrong_and_stale_atomic_responses_cannot_deliver(self):
        r = seeded()
        r.prepare_phase6_atomic()
        self.assertTrue(r.collect_phase6_atomic({"a": verdict("a")})["blocked"])
        with self.assertRaises(ValueError):
            r.collect_phase6_atomic({cid: verdict("wrong") for cid in ("a", "b", "c", "independent")})
        r.run.claims[0]["text"] = "changed while in flight"
        with self.assertRaises(ValueError):
            r.collect_phase6_atomic({cid: verdict(cid) for cid in ("a", "b", "c", "independent")})
        with self.assertRaises(ValueError):
            r.finalize()

    def test_deep_only_after_full_atomic_and_only_nonpass(self):
        r = seeded()
        with self.assertRaises(ValueError):
            r.prepare_phase6_deep_review(["a"])
        r.prepare_phase6_atomic()
        r.collect_phase6_atomic({cid: verdict(cid, "SUSPICIOUS" if cid == "a" else "PASS")
                                  for cid in ("a", "b", "c", "independent")}, model_identity="test/first")
        with self.assertRaises(ValueError):
            r.prepare_phase6_deep_review(["independent"])
        r.prepare_phase6_deep_review()
        with self.assertRaises(ValueError):
            r.collect_phase6_deep_review({"a": verdict("a")}, model_identity="test/first")
        r.collect_phase6_deep_review({"a": verdict("a")}, model_identity="test/second")
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep([])
        self.assertFalse(r.finalize()["blocked"])

    def test_linker_complete_unique_known_nonempty_or_explicit_unresolved(self):
        for links in ([], [dict(finding_index=0, claim_ids=[], unresolved=False)],
                      [dict(finding_index=0, claim_ids=["missing"], unresolved=False)]):
            r = seeded()
            findings = [dict(issue="跨章矛盾")]
            r.prepare_finding_linker(findings)
            with self.assertRaises(ValueError):
                r.apply_finding_links(findings, links)
        r = seeded()
        review_all(r)
        findings = [dict(issue="跨章矛盾")]
        r.prepare_finding_linker(findings)
        linked = r.apply_finding_links(findings, [dict(finding_index=0, claim_ids=[], unresolved=True)])
        self.assertTrue(r.prepare_phase6_revision_v4(linked)["blocked"])

    def test_global_and_source_changes_invalidate_final_delivery(self):
        r = seeded()
        review_all(r)
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep([dict(issue="stale", claim_ids=["a"])])
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep([])
        r.run.trade = {"direction": "WAIT"}
        with self.assertRaises(ValueError):
            r.finalize()

    def test_executor_rejects_local_or_same_model(self):
        r = seeded()
        first = CloudDouble()
        first.execution = "local"
        with self.assertRaises(ValueError):
            r.execute_phase6(first, CloudDouble("other"), first, first)
        first.execution = "cloud"
        with self.assertRaises(ValueError):
            r.execute_phase6(first, first, first, first)

    def test_source_add_remove_update_and_binding_mismatch(self):
        r = seeded()
        f = Fact("fact.test", 10, "USD", "FY2025", entity="TEST", concept="Revenue",
                 basis="gaap", scope="consolidated", source_id="fixture")
        r.run.fact_store.add(f)
        source = claim("source")
        source.update(author_role="bear", refs=[f.fact_id], fact_refs=[f.fact_id],
                      bindings=[dict(ref_id=f.fact_id, **{k: getattr(f, k) for k in
                                    ("entity", "concept", "period", "unit", "scope", "basis")})])
        r.orch.submit_batch(r.run_id, {"bear": [source]})
        review_all(r)
        f.value = 11
        pending = r.prepare_phase6_atomic()["atomic_prompts"]
        self.assertEqual([cid for cid, _ in pending], ["source"])
        # Removal must block final delivery, even if an LLM says PASS.
        r.run.fact_store._facts.pop(f.fact_id)
        r.prepare_phase6_atomic()
        r.collect_phase6_atomic({"source": verdict("source")})
        with self.assertRaises(ValueError):
            r.prepare_phase6_global_sweep()


if __name__ == "__main__":
    unittest.main()
