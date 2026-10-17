"""General P6 context and restart costs; no paid inference or ticker rules."""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .atomic_phase6 import build_atomic_tasks
from .checkpoint import save_checkpoint, load_checkpoint
from .journal import RunJournal
from .schema import digest
from .test_contracts import claim
from .test_atomic_phase6 import make_run
from .test_integration import seeded, CloudDouble, global_verdict


class P6Continuity(unittest.TestCase):
    def test_long_chain_context_is_local_but_ancestor_changes_invalidate_receipts(self):
        claims = [claim(f"node.{i}", [f"node.{i-1}"] if i else []) for i in range(87)]
        for c in claims:
            c["text"] = "完整推論資料" * 200
        run = make_run(claims)
        tasks = build_atomic_tasks(run)
        wire = [t.review_dict() for t in tasks]
        self.assertTrue(all(len(t["dependency_claims"]) <= 1 for t in wire))
        self.assertEqual(len(wire), len(claims))
        self.assertLess(len(json.dumps(wire)), len(json.dumps([t.to_dict() for t in tasks])) / 10)
        old_hash = wire[-1]["verification_input_hash"]
        run.claims[0]["text"] = "上游前提已修正"
        changed = build_atomic_tasks(run)[-1].review_dict()
        self.assertNotEqual(old_hash, changed["verification_input_hash"])
        self.assertEqual(changed["dependency_claims"], wire[-1]["dependency_claims"])

    def test_local_projection_preserves_typed_calc_and_event_source_closure(self):
        from .atomic_phase6 import AtomicTask
        parent = claim("parent"); parent["refs"] = ["event.release"]
        child = claim("child", ["parent"]); child["refs"] = ["calc.total"]
        catalog = {
            "calc.total": dict(kind="calc", value=dict(input_refs=["fact.input"])),
            "fact.input": dict(kind="fact", value=dict(value=42)),
            "event.release": dict(kind="event", value=dict(source_ref="fact.event", aggregate_ref="event.cap")),
            "event.cap": dict(kind="event", value=dict(source_ref="fact.cap")),
            "fact.event": dict(kind="fact", value=dict(value=1)),
            "fact.cap": dict(kind="fact", value=dict(value=2)),
            "fact.ancestor": dict(kind="fact", value=dict(value=99)),
        }
        task = AtomicTask("child", child, catalog, [parent, claim("grandparent")], [], {})
        local = task.review_dict()
        self.assertEqual(set(local["resolved_refs"]), set(catalog) - {"fact.ancestor"})
        self.assertEqual(local["resolved_refs"]["fact.input"], catalog["fact.input"])
        old = local["verification_input_hash"]
        task.resolved_refs["fact.ancestor"]["value"]["value"] = 100
        self.assertNotEqual(old, task.review_dict()["verification_input_hash"])

    def test_unchanged_execution_replays_validated_archives_without_cloud_calls(self):
        r = seeded()
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            verifier = CloudDouble()
            sweep = CloudDouble("test/global", lambda _: global_verdict(r))
            result = r.execute_phase6(verifier, CloudDouble("test/deep"), CloudDouble("test/author"), sweep)
            self.assertFalse(result["blocked"], result)
            before = (len(verifier.calls), len(sweep.calls))
            restored = load_checkpoint(Path(temp) / "checkpoint.json")
            self.assertEqual(restored._verification_epoch, r._verification_epoch)
            self.assertFalse(restored.receipts)  # checkpoint audit is never trusted
            restored.journal = RunJournal(temp)
            resumed = restored.execute_phase6(verifier, CloudDouble("test/deep"), CloudDouble("test/author"), sweep)
            self.assertFalse(resumed["blocked"], resumed)
            self.assertEqual((len(verifier.calls), len(sweep.calls)), before)
            self.assertEqual(set(restored.receipts), set(r.receipts))
            self.assertEqual(resumed["report_markdown"], result["report_markdown"])
            fresh = load_checkpoint(Path(temp) / "checkpoint.json", continue_verification=False)
            self.assertNotEqual(fresh._verification_epoch, r._verification_epoch)

    def test_canonical_change_invalidates_cached_claim_and_descendants_only(self):
        r = seeded()
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            verifier = CloudDouble()
            prepared = r.prepare_phase6_atomic()
            r._execute_review_batch(verifier, prepared["atomic_prompts"], r.collect_phase6_atomic)
            before = len(verifier.calls)
            old = {cid: digest(t.to_dict()) for cid,t in r._tasks().items()}
            restored = load_checkpoint(Path(temp) / "checkpoint.json")
            restored.journal = RunJournal(temp)
            next(c for c in restored.run.claims if c["claim_id"] == "a")["text"] = "全新前提"
            changed = {cid for cid,t in restored._tasks().items() if digest(t.to_dict()) != old[cid]}
            prepared = restored.prepare_phase6_atomic()
            result = restored._execute_review_batch(verifier, prepared["atomic_prompts"], restored.collect_phase6_atomic)
            self.assertFalse(result["blocked"], result)
            self.assertIn("b", changed)
            self.assertEqual(len(verifier.calls) - before, len(changed))

    def test_legacy_checkpoint_cannot_silently_restart_paid_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(seeded(), path)
            data = json.loads(path.read_text()); data.pop("verification_epoch")
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "explicit fresh verification"):
                load_checkpoint(path)
            self.assertTrue(load_checkpoint(path, continue_verification=False).run.claims)

    def test_contract_budget_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded(); r._contract_attempts = {"request": 3}
            path = Path(temp) / "checkpoint.json"; save_checkpoint(r, path)
            self.assertEqual(load_checkpoint(path)._contract_attempts, {"request": 3})

    def test_global_checks_all_claims_once_and_preserves_causal_revision_content(self):
        from .test_integration import review_all
        r = seeded(); review_all(r)
        _, prompt = r.prepare_phase6_global_sweep()
        payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
        self.assertNotIn("report_markdown", payload)
        self.assertEqual(payload["state"]["claims"], r.run.claims)
        self.assertEqual(payload["canonical_state_hash"], r._hash())

    def test_oversize_review_blocks_before_spending_instead_of_truncating(self):
        r = seeded(); r.review_batch_max_bytes = 1024
        verifier = CloudDouble()
        result = r.execute_phase6(verifier, CloudDouble("test/deep"), CloudDouble("test/author"), CloudDouble("test/global"))
        self.assertTrue(result["blocked"])
        self.assertIn("review_context_too_large", result["reason"])
        self.assertEqual(verifier.calls, [])
