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
import contextlib
import io
from v3.pipeline import RunState
from v3.facts import Fact
from .runner import PipelineRunnerV4, ROLES
from .schema import Claim
from .test_contracts import claim


def comparable_wait():
    return {"direction": "WAIT", "decision_comparison": {
        "research_view": {"direction": "UNKNOWN", "reason": "evidence incomplete", "horizon_end": None, "refs": []},
        "alternatives": [{"direction": side, "status": "UNASSESSABLE", "reason": "insufficient evidence", "support_refs": [], "opposition_refs": [], "missing_information": ["evidence"]} for side in ("BUY", "SHORT")],
        "wait": {"best_alternative": "UNKNOWN", "reason": "neither alternative is assessable", "reassessment_trigger": "new evidence", "refs": []}}}


class CloudDouble:
    execution = "cloud"

    def __init__(self, identity="test/atomic", responder=None):
        self.identity = identity
        self.calls = []
        self.responder = responder

    def complete(self, prompt):
        self.calls.append(prompt)
        if self.responder:
            result = self.responder(prompt)
            # Legacy scripted global findings are expanded into explicit audit
            # rows by the fixture only. Production accepts no such shortcut.
            if isinstance(result, list) and "<context-data>" in prompt:
                payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
                if payload.get("review_scope") == "global_consistency":
                    return global_fixture(payload["state"]["claims"], result)
            return result
        # Parsing test protocol JSON is not production semantic routing.
        payload = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
        if "tasks" in payload:
            return dict(results=[verdict(row["claim"]["claim_id"], ref_ids=row["expected_reference_ids"])
                                 for row in payload["tasks"]])
        return verdict(payload["claim_id"], ref_ids=reference_ids(payload["claim"]))


def reference_ids(claim):
    return list(dict.fromkeys(claim["refs"] + claim["fact_refs"] + claim["dependencies"]))


def global_fixture(claims, findings=None):
    return dict(findings=findings or [], dependency_reviews=[
        dict(claim_id=c["claim_id"], required_dependencies=c["dependencies"],
             unresolved=False, reason="scripted dependency verdict") for c in claims])


def global_verdict(r, findings=None):
    return global_fixture(r.run.claims, findings)


def verdict(cid, status="PASS", ref_ids=()):
    return dict(claim_id=cid, verdict=status,
                reference_checks=[dict(ref_id=ref, relation="SUPPORTS", attributed_assertion="fixture attribution",
                                       source_evidence="fixture evidence", reason="scripted supporting relation") for ref in ref_ids],
                findings=[] if status == "PASS" else [dict(issue="來源與斷言不一致", evidence="test", suggested_fix="局部修正")])


def seeded():
    # Legacy one-Claim protocol fixtures; dedicated batch tests cover the default.
    r = PipelineRunnerV4(review_batch_size=1, max_workers=4)
    r.run_id = "fixture"
    r.orch.runs[r.run_id] = RunState(run_id=r.run_id, ticker="TEST",
                                    manifest={"subject": {"ticker": "TEST"}, "as_of": "2026-10-16"})
    r.run.events = {}
    r.orch.register_trade(r.run_id, {"direction": "WAIT"})
    grouped = {"bull": [claim("a"), claim("b", ["a"]), claim("c", ["b"]), claim("independent")]}
    for role in ROLES:
        if role == "bull":
            continue
        c = claim("role." + role)
        c["author_role"] = role
        if role == "trader":
            c["assertion"] = dict(kind="trade_plan", trade={"direction": "WAIT"})
        grouped[role] = [c]
    r.orch.submit_batch(r.run_id, grouped)
    return r


def review_all(r):
    prepared = r.prepare_phase6_atomic()
    index = {c["claim_id"]: c for c in r.run.claims}
    return r.collect_phase6_atomic({cid: verdict(cid, ref_ids=reference_ids(index[cid])) for cid, _ in prepared["atomic_prompts"]}, model_identity="test/first")


class Integration(unittest.TestCase):
    def test_policy_failure_blocks_report_and_preserves_machine_readable_reason(self):
        from .agy_session import AntigravityError
        def denied(_):
            raise AntigravityError("permission_denied")
        r = seeded()
        verifier = CloudDouble("test/shared", denied)
        deep = CloudDouble("test/shared")
        sweep = CloudDouble("test/global", lambda _: [])
        result = r.execute_phase6(verifier, deep, CloudDouble("test/author"), sweep)
        self.assertEqual(result["status"], "blocked:cloud_policy")
        self.assertEqual(result["error_code"], "permission_denied")
        self.assertFalse(result["retryable"])
        self.assertNotIn("report_markdown", result)
        self.assertEqual(deep.calls, [])
        self.assertEqual(sweep.calls, [])

    def test_cli_transport_check_never_runs_pipeline_or_delivers_report(self):
        from .__main__ import main
        from .agy_session import AntigravityError
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as temp:
                client = CloudDouble("test/shared")
                client.check_capabilities = lambda: dict(agent_policy_verified=True, declared_tools=[], advertised_tool_count=60, user_prompt_sent=False)
                if blocked:
                    def failed_check():
                        raise AntigravityError("tool_policy_unverified")
                    client.check_capabilities = failed_check
                args = ["--check-transport", "--out", temp, "--author-model", "shared",
                        "--verifier-model", "shared", "--deep-model", "shared", "--global-model", "shared"]
                with patch("v4.__main__.AntigravityCloudClient", return_value=client), \
                     patch("v4.__main__.PipelineRunnerV4", side_effect=AssertionError("pipeline must not start")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args), 2 if blocked else 0)
                result = json.loads((Path(temp) / "result.json").read_text())
                self.assertEqual(result["blocked"], blocked)
                self.assertNotIn("report_markdown", result)
                self.assertEqual(client.calls, [])
                self.assertTrue(result["capability_check"])
                self.assertFalse(result["user_prompt_sent"])
                if blocked:
                    self.assertEqual(result["error_code"], "tool_policy_unverified")
                    self.assertFalse(result["retryable"])
                else:
                    self.assertEqual(result["status"], "transport_ready")

    def test_cli_malformed_input_replaces_prior_success_with_blocked_result(self):
        from .__main__ import main
        for source in ("--resume", "--manifest"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                out = Path(temp) / "out"
                out.mkdir()
                result_path = out / "result.json"
                result_path.write_text(json.dumps(dict(status="complete", blocked=False, report_markdown="OLD SUCCESS")))
                invalid = Path(temp) / "invalid.json"
                invalid.write_text("not JSON")
                args = [source, str(invalid), "--out", str(out), "--author-model", "author",
                        "--verifier-model", "first", "--deep-model", "second", "--global-model", "global"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args), 2)
                result = json.loads(result_path.read_text())
                self.assertTrue(result["blocked"])
                self.assertEqual(result["status"], "blocked:input")
                self.assertNotIn("report_markdown", result)

    def test_cli_checkpoint_and_output_write_failures_are_explicit(self):
        from .__main__ import main
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity="test/global")
        completed = r.finalize()
        # Storage failures must be testable without an installed cloud CLI.
        with tempfile.TemporaryDirectory() as temp, \
             patch("v4.cloud.shutil.which", return_value=None), \
             patch("v4.__main__.AntigravityCloudClient",
                   side_effect=lambda model, **kwargs: CloudDouble("test/" + model)):
            args = ["--resume", "unused.json", "--out", temp, "--author-model", "author",
                    "--verifier-model", "first", "--deep-model", "second", "--global-model", "global"]
            with patch("v4.__main__.load_checkpoint", return_value=r), \
                 patch.object(r, "execute_phase6", return_value=completed), \
                 patch("v4.journal.save_checkpoint", side_effect=OSError("checkpoint unavailable")) as checkpoint_write, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)
                checkpoint_write.assert_called_once_with(r, Path(temp) / "checkpoint.json")
            saved = json.loads((Path(temp) / "result.json").read_text())
            self.assertEqual(saved["status"], "blocked:checkpoint")
            self.assertIn("checkpoint unavailable", saved["reason"])
            self.assertNotIn("report_markdown", saved)
            stdout = io.StringIO()
            with patch("v4.__main__.load_checkpoint", side_effect=ValueError("input invalid")), \
                 patch("v4.__main__.write_json", side_effect=PermissionError("output denied")), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(main(args), 2)
            self.assertFalse(json.loads(stdout.getvalue())["result_written"])
            self.assertEqual(json.loads(stdout.getvalue())["status"], "blocked:output")

    def test_manual_collect_requires_model_identity_but_allows_same_model_deep_review(self):
        r = seeded()
        prepared = r.prepare_phase6_atomic()
        results = {cid: verdict(cid, "FAIL" if cid == "a" else "PASS", ref_ids=reference_ids(next(c for c in r.run.claims if c["claim_id"] == cid))) for cid, _ in prepared["atomic_prompts"]}
        for identity in (None, "", False, " "):
            with self.subTest(stage="atomic", identity=identity), self.assertRaises(ValueError):
                r.collect_phase6_atomic(results, model_identity=identity)
            self.assertEqual(r.receipts, {})
        r.collect_phase6_atomic(results, model_identity="test/first")
        r.prepare_phase6_deep_review()
        for identity in (None, "", False, " "):
            with self.subTest(stage="deep", identity=identity), self.assertRaises(ValueError):
                r.collect_phase6_deep_review({"a": verdict("a")}, model_identity=identity)
            self.assertEqual(r.deep_receipts, {})
        r.collect_phase6_deep_review({"a": verdict("a")}, model_identity="test/first")
        r.prepare_phase6_global_sweep()
        for identity in (None, "", False, " "):
            with self.subTest(stage="global", identity=identity), self.assertRaises(ValueError):
                r.collect_phase6_global_sweep(global_verdict(r), model_identity=identity)
            self.assertIsNone(r.global_receipt)
        r.collect_phase6_global_sweep(global_verdict(r), model_identity="test/global")
        self.assertFalse(r.finalize()["blocked"])

    def test_partial_role_checkpoint_never_skips_missing_roles_on_resume(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        for completed in ((), ROLES[:4], ROLES[:6], ROLES[:7], ROLES[:8]):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as temp:
                r = seeded()
                r.run.claims = [c for c in r.run.claims if c["author_role"] in completed]
                if "trader" not in completed:
                    r.run.trade = {}
                r.run.stage = "blocked:roles"
                r.orch._sync_views(r.run)
                path = Path(temp) / "partial.json"
                save_checkpoint(r, path)
                restored = load_checkpoint(path)
                first, deep = CloudDouble(), CloudDouble("test/deep")
                sweep = CloudDouble("test/global", lambda _: [])
                result = restored.execute_phase6(first, deep, sweep, sweep)
                self.assertTrue(result["blocked"])
                self.assertIn("incomplete role", result["reason"])
                self.assertNotIn("report_markdown", result)
                self.assertEqual(first.calls, [])
                with self.assertRaises(ValueError):
                    restored.finalize()

    def test_trade_plan_cannot_be_removed_and_failed_patch_is_transactional(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded()
        review_all(r)
        before = r.orch.snapshot(r.run_id)
        receipts = copy.deepcopy(r.receipts)
        r.prepare_phase6_revision_v4([dict(claim_id="role.trader", issue="invalid retype")])
        with self.assertRaises(ValueError):
            r.collect_phase6_revision([dict(claim_id="role.trader", assertion=dict(kind="interpretation"))])
        self.assertEqual(r.orch.snapshot(r.run_id), before)
        self.assertEqual(r.receipts, receipts)
        self.assertEqual(r.history, [])
        # A tampered checkpoint must hit the same invariant as initial inputs,
        # patch candidates and final validation.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            save_checkpoint(r, path)
            data = json.loads(path.read_text())
            for c in data["state"]["claims"]:
                if c["claim_id"] == "role.trader":
                    c["assertion"] = dict(kind="interpretation")
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_checkpoint(path)

    def test_cli_success_and_blocked_run_replace_single_delivery_artifact(self):
        from .__main__ import main
        def author_response(prompt):
            packet = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
            c = claim(packet["role"] + ".1")
            c["author_role"] = packet["role"]
            output = {"claims": [c]}
            if packet["role"] == "trader":
                output["trade"] = comparable_wait()
                c["assertion"] = dict(kind="trade_plan", trade=output["trade"])
            return output
        clients = {"author": CloudDouble("test/author", author_response), "atomic": CloudDouble(),
                   "deep": CloudDouble("test/deep"), "global": CloudDouble("test/global", lambda _: [])}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "news.json"
            path.write_text(json.dumps({"items": [dict(title="test", url="https://example.test/source", published="2026-10-15")]}))
            manifest_path = Path(temp) / "manifest.json"
            manifest_path.write_text(json.dumps(dict(subject={"ticker": "TEST"}, as_of="2026-10-16",
                                                     sources=[dict(id="news", type="news_json", path=str(path))])))
            out = Path(temp) / "out"
            args = ["--manifest", str(manifest_path), "--out", str(out), "--author-model", "author",
                    "--verifier-model", "atomic", "--deep-model", "deep", "--global-model", "global"]
            with patch("v4.__main__.AntigravityCloudClient", side_effect=lambda model, **kwargs: clients[model]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                self.assertIn("report_markdown", json.loads((out / "result.json").read_text()))
                self.assertTrue((out / "checkpoint.json").exists())
                clients["global"].responder = lambda _: "not JSON"
                self.assertEqual(main(args), 2)
                blocked = json.loads((out / "result.json").read_text())
                self.assertTrue(blocked["blocked"])
                self.assertNotIn("report_markdown", blocked)

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
                output["trade"] = comparable_wait()
                c["assertion"] = dict(kind="trade_plan", trade=output["trade"])
            return output
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "news.json"
            path.write_text(json.dumps({"items": [dict(title="來源測試", url="https://example.test/source", published="2026-10-15")]}))
            manifest = dict(subject={"ticker": "TEST"}, as_of="2026-10-16",
                            sources=[dict(id="news", type="news_json", path=str(path))])
            r = PipelineRunnerV4(review_batch_size=4)
            author = CloudDouble("test/author", author_response)
            atomic, deep = CloudDouble(), CloudDouble("test/deep")
            sweep = CloudDouble("test/global", lambda _: [])
            result = r.execute("TEST", manifest, author, atomic, deep, sweep)
            self.assertFalse(result["blocked"], result)
            self.assertEqual(len(author.calls), len(ROLES))
            self.assertEqual(len(atomic.calls), 3)
            self.assertEqual(set(r.receipts), {c["claim_id"] for c in r.run.claims})
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
            return verdict(task["claim_id"], status, ref_ids=reference_ids(task["claim"]))
        atomic = CloudDouble("test/atomic", atomic_response)
        deep = CloudDouble("test/deep", lambda _: verdict("a", "FAIL"))
        author = CloudDouble("test/author", lambda _: [dict(claim_id="a", text="修正版")])
        sweep = CloudDouble("test/global", lambda _: [])
        result = r.execute_phase6(atomic, deep, author, sweep)
        self.assertFalse(result["blocked"], result)
        self.assertEqual(len(atomic.calls), 15)
        self.assertEqual(len(deep.calls), 1)
        self.assertEqual(len(sweep.calls), 1)
        self.assertEqual(result["sidecar"]["revision_history"][0]["affected_claim_ids"], ["a", "b", "c"])

    def test_same_model_runs_separate_deep_request_to_overturn_or_confirm_failure(self):
        for deep_status in ("PASS", "FAIL"):
            with self.subTest(deep_status=deep_status):
                r, atomic_ids, deep_ids = seeded(), [], []
                def respond(prompt):
                    if "```json\n" in prompt:
                        task = json.loads(prompt.split("```json\n")[1].split("\n```\n")[0])
                        cid = task["claim_id"]
                        atomic_ids.append(cid)
                        status = "FAIL" if cid == "b" and task["claim"]["text"] != "修正版" else "PASS"
                        return verdict(cid, status, ref_ids=reference_ids(task["claim"]))
                    payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
                    self.assertEqual(payload["first_review"]["verdict"], "FAIL")
                    deep_ids.append(payload["task"]["claim_id"])
                    return verdict("b", deep_status, ref_ids=["a"])
                shared = CloudDouble("test/shared", respond)
                patcher = CloudDouble("test/author", lambda _: [dict(claim_id="b", text="修正版")])
                sweep = CloudDouble("test/global", lambda _: [])
                result = r.execute_phase6(shared, shared, patcher, sweep)
                self.assertFalse(result["blocked"], result)
                self.assertEqual(deep_ids, ["b"])
                self.assertEqual(set(atomic_ids), {c["claim_id"] for c in r.run.claims})
                self.assertEqual(len(shared.calls), len(atomic_ids) + 1)
                self.assertEqual(len(sweep.calls), 1)
                self.assertTrue(all(x["model"] == "test/shared" for x in r.receipts.values()))
                if deep_status == "PASS":
                    self.assertEqual(len(atomic_ids), len(r.run.claims))
                    self.assertEqual(patcher.calls, [])
                    self.assertEqual(r.deep_receipts["b"]["model"], "test/shared")
                else:
                    self.assertEqual(len(patcher.calls), 1)
                    self.assertEqual(atomic_ids.count("b"), 2)
                    self.assertEqual(atomic_ids.count("c"), 2)
                    self.assertEqual(len(atomic_ids), len(r.run.claims) + 2)
                    self.assertEqual(r.history[0]["affected_claim_ids"], ["b", "c"])

    def test_cli_resume_accepts_same_model_for_verifier_and_deep(self):
        from .__main__ import main
        from .checkpoint import save_checkpoint
        r = seeded()
        def respond(prompt):
            if "```json\n" in prompt:
                task = json.loads(prompt.split("```json\n")[1].split("\n```\n")[0])
                return verdict(task["claim_id"], "SUSPICIOUS" if task["claim_id"] == "b" else "PASS",
                               ref_ids=reference_ids(task["claim"]))
            return verdict("b", ref_ids=["a"])
        shared = CloudDouble("test/shared", respond)
        clients = {"shared": shared, "author": CloudDouble("test/author"),
                   "global": CloudDouble("test/global", lambda _: [])}
        with tempfile.TemporaryDirectory() as temp:
            checkpoint, out = Path(temp) / "checkpoint.json", Path(temp) / "out"
            save_checkpoint(r, checkpoint)
            args = ["--resume", str(checkpoint), "--out", str(out), "--author-model", "author",
                    "--verifier-model", "shared", "--deep-model", "shared", "--global-model", "global"]
            with patch("v4.__main__.AntigravityCloudClient", side_effect=lambda model, **kwargs: clients[model]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            result = json.loads((out / "result.json").read_text())
            self.assertFalse(result["blocked"], result)
            self.assertIn("report_markdown", result)
            self.assertEqual(len(shared.calls), len(r.run.claims) + 1)
            saved = json.loads((out / "checkpoint.json").read_text())
            self.assertEqual(saved["audit"]["deep"]["b"]["model"], "test/shared")
            archives = [json.loads(path.read_text()) for path in (out / "responses" / "archive").glob("*.json")]
            reviews = [entry for entry in archives if entry["task_id"] == "b"]
            self.assertEqual(len(reviews), 2)
            self.assertEqual({entry["model"] for entry in reviews}, {"test/shared"})
            self.assertEqual(len({entry["prompt_hash"] for entry in reviews}), 2)
            self.assertTrue(all(entry["status"] == "received" for entry in reviews))
            self.assertEqual({entry["response"]["verdict"] for entry in reviews}, {"SUSPICIOUS", "PASS"})

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
        c.update(author_role="event_analyst", text="完全無關的文字仍由結構檢查", refs=[event["event_id"]],
                 assertion=dict(kind="event", event_ref=event["event_id"], is_aggregate=False),
                 bindings=[{k: event[k] for k in ("entity", "concept", "period", "unit", "scope", "basis")}])
        c["bindings"][0]["ref_id"] = event["event_id"]
        with self.assertRaisesRegex(ValueError, 'event assertion requires a registered matching event'):
            r.orch.submit_batch(r.run_id, {"event_analyst": [c]})
        # Old checkpoints can still reach the final deterministic guard for repair.
        r.orch.submit_batch(r.run_id, {"event_analyst": [c]}, validate_events=False)
        findings, blocked = r.orch.validate_stage(r.run_id, "final")
        self.assertTrue(blocked)
        self.assertTrue(any(f.get("check_id") == "event_binding" for f in findings))
        member = {**event, "event_id": "event.member", "is_aggregate": False, "value": 101, "aggregate_ref": "event.cap"}
        r.orch.register_events(r.run_id, [event, member])
        findings, _ = r.orch.validate_stage(r.run_id, "final")
        self.assertTrue(any(f.get("check_id") == "event_total" for f in findings))

    def test_event_catalog_and_binding_survive_checkpoint_roundtrip(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded()
        source = Fact("fact.release", 100, "SHARES", "2026", concept="Release", entity="TEST", source_id="fixture")
        r.run.fact_store.add(source)
        event = dict(event_id="event.cap", date="2026-11-01", source_ref=source.fact_id,
                     entity="TEST", concept="Release", period="2026", unit="SHARES",
                     scope="combined", basis="reported", is_aggregate=True, value=100)
        r.orch.register_events(r.run_id, [event])
        c = claim("event.claim")
        c.update(author_role="event_analyst", refs=[event["event_id"]],
                 assertion=dict(kind="event", event_ref=event["event_id"], is_aggregate=True),
                 bindings=[dict(ref_id=event["event_id"], **{key: event[key] for key in
                                ("entity", "concept", "period", "unit", "scope", "basis")})])
        r.orch.submit_batch(r.run_id, {"event_analyst": [c]})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.json"
            save_checkpoint(r, path)
            restored = load_checkpoint(path)
            self.assertEqual(restored.run.events, r.run.events)
            self.assertEqual(restored.run.claims, r.run.claims)
            atomic, deep = CloudDouble(), CloudDouble("test/deep")
            sweep = CloudDouble("test/global", lambda _: [])
            result = restored.execute_phase6(atomic, deep, sweep, sweep)
            self.assertFalse(result["blocked"], result)
            self.assertEqual(result["sidecar"]["events"], r.run.events)

    def test_typed_calculation_dag_and_mismatch(self):
        from .pipeline import calculation_findings
        r = seeded()
        for fid, value, period in (("start", 10, "2025"), ("end", 20, "2026")):
            r.run.fact_store.add(Fact(fid, value, "USD", period, concept="Amount", source_id="fixture"))
        r.run.calc_store.add("first", 1.0, "growth", ["end", "start"])
        r.run.calc_store.add("second", 19.0, "fcf", ["end", "first"])
        self.assertFalse(any(f["status"] == "FAIL" for f in calculation_findings(r.run)))
        r.run.calc_store._calcs["first"]["value"] = 2
        self.assertTrue(any(f["status"] == "FAIL" for f in calculation_findings(r.run)))

    def test_trade_claim_patch_regenerates_numeric_resources_and_risk_context(self):
        r = seeded()
        original = dict(direction="BUY", entry=100, stop=90, target=120, position_pct=10)
        r.orch.register_trade(r.run_id, original)
        plan_id, risk_id = "role.trader", "role.risk_manager"
        for c in r.run.claims:
            if c["claim_id"] == plan_id:
                c.update(dependencies=["a"], assertion=dict(kind="trade_plan", trade=original))
            if c["claim_id"] == risk_id:
                c["dependencies"] = [plan_id]
        r.orch._sync_views(r.run)
        review_all(r)
        r.prepare_phase6_revision_v4([dict(claim_id=plan_id, issue="change plan")])
        updated = {**original, "stop": 95}
        pending = r.collect_phase6_revision([dict(claim_id=plan_id, assertion=dict(kind="trade_plan", trade=updated))])
        self.assertEqual(r.run.trade, updated)
        self.assertEqual(r.run.fact_store.get("fact.Trade.Stop").value, 95)
        self.assertEqual(r.run.calc_store.get("calc.trade.risk_reward")["value"], 4)
        self.assertEqual(set(pending["revalidate_claim_ids"]), {plan_id, risk_id})
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
            self.assertEqual(len(first.calls), 12)
            self.assertEqual(deep.calls, [])
            self.assertEqual(len(sweep.calls), 1)

    def test_patch_invalidates_descendants_and_syncs_every_view(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity="test/global")
        r.prepare_phase6_revision_v4([dict(claim_id="a", issue="fix")])
        result = r.collect_phase6_revision([dict(claim_id="a", text="修正版")])
        self.assertEqual(result["revalidate_claim_ids"], ["a", "b", "c"])
        self.assertEqual({cid for cid, _ in result["atomic_prompts"]}, {"a", "b", "c"})
        self.assertEqual(set(r.receipts), {c["claim_id"] for c in r.run.claims} - {"a", "b", "c"})
        self.assertEqual(r.run.analyses["bull"]["claims"][0]["text"], "修正版")
        self.assertEqual(r.orch.get_packet(r.run_id, "trader")["claims"][0]["text"], "修正版")
        with self.assertRaises(ValueError):
            r.finalize()
        r.collect_phase6_atomic({cid: verdict(cid, ref_ids=reference_ids(next(c for c in r.run.claims if c["claim_id"] == cid))) for cid, _ in result["atomic_prompts"]}, model_identity="test/first")
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity="test/global")
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
        self.assertTrue(r.collect_phase6_atomic({"a": verdict("a")}, model_identity="test/first")["blocked"])
        with self.assertRaises(ValueError):
            r.collect_phase6_atomic({c["claim_id"]: verdict("wrong") for c in r.run.claims}, model_identity="test/first")
        r.run.claims[0]["text"] = "changed while in flight"
        with self.assertRaises(ValueError):
            r.collect_phase6_atomic({c["claim_id"]: verdict(c["claim_id"]) for c in r.run.claims}, model_identity="test/first")
        with self.assertRaises(ValueError):
            r.finalize()

    def test_deep_only_after_full_atomic_and_only_nonpass(self):
        r = seeded()
        with self.assertRaises(ValueError):
            r.prepare_phase6_deep_review(["a"])
        r.prepare_phase6_atomic()
        r.collect_phase6_atomic({c["claim_id"]: verdict(c["claim_id"], "SUSPICIOUS" if c["claim_id"] == "a" else "PASS", ref_ids=reference_ids(c))
                                  for c in r.run.claims}, model_identity="test/first")
        with self.assertRaises(ValueError):
            r.prepare_phase6_deep_review(["independent"])
        with self.assertRaises(ValueError):
            r.prepare_phase6_global_sweep()
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_deep_review()
        r.collect_phase6_deep_review({"a": verdict("a")}, model_identity="test/first")
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity="test/global")
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
        r.collect_phase6_global_sweep(global_verdict(r, [dict(issue="stale", claim_ids=["a"])]), model_identity="test/global")
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity="test/global")
        r.run.manifest["as_of"] = "2026-10-17"
        with self.assertRaises(ValueError):
            r.finalize()

    def test_executor_rejects_local_or_unidentified_clients(self):
        for role in ("verifier", "deep", "patcher", "global", "linker"):
            for execution, identity in (("local", "test/shared"), ("cloud", None), ("cloud", " ")):
                with self.subTest(role=role, execution=execution, identity=identity):
                    clients = {name: CloudDouble("test/shared") for name in
                               ("verifier", "deep", "patcher", "global", "linker")}
                    clients[role].execution = execution
                    clients[role].identity = identity
                    with self.assertRaises(ValueError):
                        seeded().execute_phase6(*clients.values())

    def test_source_add_remove_update_and_binding_mismatch(self):
        r = seeded()
        f = Fact("fact.test", 10, "USD", "FY2025", entity="TEST", concept="Revenue",
                 basis="gaap", scope="consolidated", source_id="fixture")
        r.run.fact_store.add(f)
        source = claim("source")
        source.update(author_role="source_analyst", refs=[f.fact_id], fact_refs=[f.fact_id],
                      bindings=[dict(ref_id=f.fact_id, **{k: getattr(f, k) for k in
                                    ("entity", "concept", "period", "unit", "scope", "basis")})])
        r.orch.submit_batch(r.run_id, {"source_analyst": [source]})
        review_all(r)
        f.value = 11
        pending = r.prepare_phase6_atomic()["atomic_prompts"]
        self.assertEqual([cid for cid, _ in pending], ["source"])
        # Removal must block final delivery, even if an LLM says PASS.
        r.run.fact_store._facts.pop(f.fact_id)
        r.prepare_phase6_atomic()
        r.collect_phase6_atomic({"source": verdict("source", ref_ids=["fact.test"])}, model_identity="test/first")
        with self.assertRaises(ValueError):
            r.prepare_phase6_global_sweep()



class AdjustableRuntimeLimits(unittest.TestCase):
    def test_cli_resume_forwards_limits_and_preserves_revision_progress(self):
        from .__main__ import main
        from .checkpoint import save_checkpoint
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            runner = seeded(); runner.revision_round = 2
            checkpoint = Path(temp) / "checkpoint.json"; save_checkpoint(runner, checkpoint)
            argv = ["--resume", str(checkpoint), "--out", str(Path(temp) / "out"),
                    "--session-max-continuations", "12", "--session-budget-seconds", "14400",
                    "--max-revisions", "40", "--max-contract-attempts", "8"]
            for role in ("author", "verifier", "deep", "global"):
                argv += [f"--{role}-model", "model"]
            observed = []
            def resume(self, *clients):
                observed.append((self.max_revisions, self.revision_round, self.max_contract_attempts))
                return dict(blocked=True, status="test_pause")
            with patch("v4.__main__.AntigravityCloudClient", return_value=CloudDouble()) as client, \
                 patch.object(PipelineRunnerV4, "resume", autospec=True, side_effect=resume):
                self.assertEqual(main(argv), 2)
            self.assertEqual(observed, [(40, 2, 8)])
            self.assertEqual(client.call_args.kwargs["max_continuations"], 12)
            self.assertEqual(client.call_args.kwargs["session_budget_seconds"], 14400)


class BindingReferenceContracts(unittest.TestCase):
    def test_exact_source_index_uses_catalog_type_without_prefix_guessing(self):
        runner = seeded()
        fact = Fact('opaque-ledger-row', 10, 'USD', 'FY2025', entity='TEST', concept='Revenue',
                    basis='gaap', scope='consolidated', source_id='fixture')
        runner.run.fact_store.add(fact)
        packet = runner.orch.get_packet(runner.run_id, 'fundamentals_analyst')
        self.assertEqual(packet['source_index'][fact.fact_id], 'fact')
        source = claim('opaque.user')
        source.update(author_role='source_analyst', refs=[fact.fact_id], fact_refs=[fact.fact_id],
            bindings=[dict(ref_id=fact.fact_id, **{key: getattr(fact, key) for key in
                ('entity', 'concept', 'period', 'unit', 'scope', 'basis')})])
        runner.orch.submit_batch(runner.run_id, {'source_analyst': [source]})
        self.assertEqual(runner.receipts, {})
        fact.value = 11
        self.assertEqual(runner.orch.get_packet(runner.run_id, 'fundamentals_analyst')['facts'][fact.fact_id]['value'], 11)
        runner.run.fact_store._facts.pop(fact.fact_id)
        self.assertNotIn(fact.fact_id, runner.orch.get_packet(runner.run_id, 'fundamentals_analyst')['source_index'])

    def fixture(self):
        from .schema import DIMENSIONS
        runner = seeded()
        fact = Fact("fact.binding", 10, "USD", "FY2025", entity="TEST", concept="Revenue",
                    basis="gaap", scope="consolidated", source_id="fixture")
        runner.run.fact_store.add(fact)
        c = claim("binding_user.conclusion", ["a"])
        c.update(author_role="binding_user", refs=[fact.fact_id, "a"], fact_refs=[fact.fact_id],
                 bindings=[dict(ref_id=fact.fact_id, **{key: getattr(fact, key) for key in DIMENSIONS}),
                           dict(ref_id="a", **{key: "unknown" for key in DIMENSIONS})])
        return runner, c

    def test_fact_reference_in_refs_is_checked_even_without_redundant_fact_refs(self):
        runner, c = self.fixture()
        c['fact_refs'] = []
        runner.orch.submit_batch(runner.run_id, {'binding_user': [c]})
        prepared = runner.prepare_phase6_atomic()
        cid = c['claim_id']; schema = prepared['response_schemas'][cid]
        self.assertIn('fact.binding', schema['properties']['reference_checks']['items']['properties']['ref_id']['enum'])
        results = {row['claim_id']: verdict(row['claim_id'], ref_ids=reference_ids(row)) for row in runner.run.claims}
        results[cid]['reference_checks'] = [row for row in results[cid]['reference_checks'] if row['ref_id'] != 'fact.binding']
        summary = runner.collect_phase6_atomic(results, model_identity='test/verifier')
        self.assertTrue(summary['blocked'])
        self.assertIn(cid, summary['invalid_claim_ids'])

    def test_source_and_claim_bindings_are_preserved_and_both_semantically_checked(self):
        runner, c = self.fixture()
        runner.orch.submit_batch(runner.run_id, {"binding_user": [c]})
        stored = next(row for row in runner.run.claims if row["claim_id"] == c["claim_id"])
        self.assertEqual(stored["bindings"], c["bindings"])
        findings, _ = runner.orch.validate_stage(runner.run_id, "final")
        self.assertFalse(any(f.get("status") == "FAIL" and f.get("ref_id") == "a" for f in findings))
        prepared = runner.prepare_phase6_atomic()
        cid = c["claim_id"]
        self.assertIn(cid, [key for key, _ in prepared["atomic_prompts"]])
        schema = prepared["response_schemas"][cid]
        self.assertEqual(set(schema["properties"]["reference_checks"]["items"]["properties"]["ref_id"]["enum"]),
                         {"a", "fact.binding"})
        results = {row["claim_id"]: verdict(row["claim_id"], ref_ids=reference_ids(row)) for row in runner.run.claims}
        results[cid]["reference_checks"] = [row for row in results[cid]["reference_checks"] if row["ref_id"] != "a"]
        summary = runner.collect_phase6_atomic(results, model_identity="test/verifier")
        self.assertTrue(summary["blocked"])
        self.assertIn(cid, summary["invalid_claim_ids"])

    def test_role_prompt_output_with_claim_binding_reaches_next_native_phase(self):
        runner, c = self.fixture()
        c.update(claim_id="research_manager.conclusion", author_role="research_manager")
        runner.run.claims = [row for row in runner.run.claims if row["author_role"] not in
                             {"research_manager", "trader", "risk_manager"}]
        runner.run.analyses = {role: value for role, value in runner.run.analyses.items() if role not in
                               {"research_manager", "trader", "risk_manager"}}
        runner.orch._sync_views(runner.run)
        author = CloudDouble("test/author", lambda _: dict(claims=[c]))
        role, prompt = runner._role_prompts(("research_manager",))[0]
        response = runner._complete(author, role, prompt, reuse_role_output=True)
        next_role, _ = runner.collect_rm(response["claims"])
        self.assertEqual(next_role, "trader")
        self.assertEqual(next(row for row in runner.run.claims if row["claim_id"] == c["claim_id"])["bindings"], c["bindings"])

    def test_optional_claim_binding_does_not_weaken_source_or_dependency_constraints(self):
        for mutation in ("missing_source", "duplicate_source", "duplicate_claim", "unknown", "missing_dependency"):
            with self.subTest(mutation=mutation):
                runner, c = self.fixture()
                if mutation == "missing_source": c["bindings"] = c["bindings"][1:]
                elif mutation == "duplicate_source": c["bindings"].append(copy.deepcopy(c["bindings"][0]))
                elif mutation == "duplicate_claim": c["bindings"].append(copy.deepcopy(c["bindings"][1]))
                elif mutation == "unknown":
                    c["refs"][-1] = "missing"; c["bindings"][-1]["ref_id"] = "missing"
                else: c["dependencies"] = []
                before = copy.deepcopy(runner.run.claims)
                with self.assertRaises(ValueError): runner.orch.submit_batch(runner.run_id, {"binding_user": [c]})
                self.assertEqual(runner.run.claims, before)

    def test_real_source_binding_mismatch_still_fails_and_claim_binding_is_optional(self):
        for include_claim in (True, False):
            with self.subTest(include_claim=include_claim):
                runner, c = self.fixture()
                if not include_claim: c["bindings"].pop()
                c["bindings"][0]["unit"] = "shares"
                runner.orch.submit_batch(runner.run_id, {"binding_user": [c]})
                findings, blocked = runner.orch.validate_stage(runner.run_id, "final")
                self.assertTrue(blocked)
                self.assertTrue(any(f.get("status") == "FAIL" and f.get("ref_id") == "fact.binding"
                                    and f.get("dimension") == "unit" for f in findings))

    def test_claim_bindings_survive_checkpoint_reload_and_surgical_patching(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        runner, c = self.fixture()
        runner.orch.submit_batch(runner.run_id, {"binding_user": [c]})
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint.json"; save_checkpoint(runner, checkpoint)
            restored = load_checkpoint(checkpoint)
        self.assertEqual(next(row for row in restored.run.claims if row["claim_id"] == c["claim_id"])["bindings"], c["bindings"])
        restored.orch.patch(restored.run_id, [dict(claim_id=c["claim_id"], text="Updated conclusion")], [c["claim_id"]])
        self.assertEqual(next(row for row in restored.run.claims if row["claim_id"] == c["claim_id"])["bindings"], c["bindings"])

if __name__ == "__main__":
    unittest.main()
