"""Crash/restart tests exercise disk artifacts, not real cloud requests."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from .checkpoint import load_checkpoint, save_checkpoint
from .journal import RunJournal
from .runner import PHASE_2_ROLES
from .test_contracts import claim
from .test_integration import seeded, CloudDouble, global_verdict


def author_response(prompt):
    packet = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
    role = packet["role"]
    c = claim(role + ".restart")
    c["author_role"] = role
    result = dict(claims=[c])
    if role == "trader":
        result["trade"] = dict(direction="WAIT")
        c["assertion"] = dict(kind="trade_plan", trade=result["trade"])
    return result


class Journal(unittest.TestCase):
    def test_grounding_error_outside_role_generation_does_not_suggest_regenerating_committed_role(self):
        from .pipeline import ClaimSourceError
        r = seeded()
        c = claim('bad.patch'); c['claim_type'] = 'source_fact'
        result = r._blocked(ClaimSourceError(c), 'blocked:revision_patch')
        self.assertNotIn('retry_role', result)
        self.assertEqual(result['next_action'], 'inspect_claim_input')
        self.assertEqual(result['role_validation']['claim_id'], 'bad.patch')

    def test_role_notes_reach_atomic_and_completed_reviews_survive_restart(self):
        r = seeded(); r.run.claims = []; r.run.analyses = {}
        def author_with_notes(prompt):
            output = author_response(prompt)
            c = output['claims'][0]
            if c['author_role'] == 'fundamentals_analyst':
                c.update(claim_id='fundamentals_analyst.data_limitations_and_risks',
                         claim_type='semantic_fact', text='本次輸入資料的充分性仍需確認。')
            elif c['author_role'] == 'research_manager':
                c.update(claim_id='research_manager.001', claim_type='semantic_fact',
                         text='目前無法確認是否有傳輸截斷，不能把猜測當成原因。',
                         refs=['market_analyst.restart'], dependencies=['market_analyst.restart'])
            return output
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            author = CloudDouble('test/author', author_with_notes)
            verifier = CloudDouble()
            deep = CloudDouble('test/deep')
            sweep = CloudDouble('test/global', lambda _: global_verdict(r))
            result = r.resume(author, verifier, deep, sweep)
            self.assertFalse(result['blocked'], result)
            self.assertEqual(len(author.calls), 9)
            self.assertEqual(len(verifier.calls), len(r.run.claims))
            self.assertEqual(deep.calls, [])
            notes = {'fundamentals_analyst.data_limitations_and_risks', 'research_manager.001'}
            self.assertTrue(notes <= r.receipts.keys())
            self.assertTrue(r.global_receipt)
            resumed = load_checkpoint(Path(temp) / 'checkpoint.json')
            resumed.journal = RunJournal(temp)
            sweep.responder = lambda _: global_verdict(resumed)
            self.assertFalse(resumed.resume(author, verifier, deep, sweep)['blocked'])
            self.assertEqual(len(author.calls), 9)
            self.assertEqual(len(verifier.calls), len(r.run.claims))

    def test_policy_failure_is_durable_and_resume_never_replays_failed_response(self):
        from .agy_session import AntigravityError
        r = seeded()
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            client = CloudDouble("test/shared", lambda _: (_ for _ in ()).throw(AntigravityError("tool_policy_unverified")))
            with self.assertRaises(AntigravityError):
                r._complete(client, "role", "secret prompt", reuse_role_output=True)
            archives = [json.loads(path.read_text()) for path in (Path(temp) / "responses/archive").glob("*.json")]
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0]["status"], "failed")
            self.assertEqual(archives[0]["error_code"], "tool_policy_unverified")
            self.assertFalse(archives[0]["retryable"])
            self.assertNotIn("response", archives[0])
            self.assertNotIn("secret", json.dumps(archives))
            client.responder = lambda _: dict(ok=True)
            with self.assertRaises(AntigravityError):
                r._complete(client, "role", "secret prompt", reuse_role_output=True)
            self.assertEqual(len(client.calls), 1)
            r.journal.retry_roles.add("role")
            self.assertEqual(r._complete(client, "role", "secret prompt", reuse_role_output=True), dict(ok=True))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(len(list((Path(temp) / "responses/archive").glob("*.json"))), 2)

    def test_invalid_json_correction_and_success_replay_preserve_journal_session(self):
        from .cloud import AntigravityCloudClient
        r = seeded()
        with tempfile.TemporaryDirectory() as temp, patch("v4.cloud.shutil.which", return_value="/test/agy"), \
             patch("v4.cloud.run_session", side_effect=["not JSON", '{"ok":true}']) as run:
            r.journal = RunJournal(temp)
            client = AntigravityCloudClient("model")
            self.assertEqual(r._complete(client,"bull","prompt",reuse_role_output=True),dict(ok=True))
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args_list[0].kwargs['session_dir'],run.call_args_list[1].kwargs['session_dir'])
            self.assertTrue(run.call_args_list[1].kwargs['repair_json'])
            r.journal = RunJournal(temp)
            self.assertEqual(r._complete(client,"bull","prompt",reuse_role_output=True),dict(ok=True))
            self.assertEqual(run.call_count,2)

    def test_structured_schema_changes_journal_identity_and_is_forwarded(self):
        class StructuredDouble(CloudDouble):
            def complete(self, _):
                raise AssertionError("structured request must use structured capability")

            def complete_structured(self, prompt, schema):
                self.calls.append((prompt, copy.deepcopy(schema)))
                return dict(ok=True)
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            client = StructuredDouble()
            for schema in (dict(type="object"), dict(type="array")):
                self.assertEqual(r._complete(client, "task", "same prompt", response_schema=schema), dict(ok=True))
            records = [json.loads(p.read_text()) for p in (Path(temp) / "responses").glob("*.json")]
            self.assertEqual(len(records), 2)
            self.assertEqual(len({rec["response_schema_hash"] for rec in records}), 2)
            self.assertEqual([schema for _, schema in client.calls], [dict(type="object"), dict(type="array")])

    def test_archive_recovers_current_attempt_when_index_write_fails(self):
        from .checkpoint import write_json
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            client = CloudDouble("test/author", lambda _: dict(claims="saved reply"))
            def fail_index(path, data):
                if data.get("status") == "received" and Path(path).parent.name == "responses":
                    raise OSError("interrupted index write")
                write_json(path, data)
            with patch("v4.journal.write_json", side_effect=fail_index), self.assertRaises(OSError):
                r._complete(client, "bull", "same prompt", reuse_role_output=True)
            r.journal = RunJournal(temp)
            recovered = r._complete(client, "bull", "same prompt", reuse_role_output=True)
            self.assertEqual(recovered, dict(claims="saved reply"))
            self.assertEqual(len(client.calls), 1)

    def test_incomplete_retry_never_recovers_older_attempt_or_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source", Path(temp) / "target"
            r = seeded()
            client = CloudDouble("test/author", lambda _: dict(claims="old invalid reply"))
            r.journal = RunJournal(source)
            r._complete(client, "bull", "same prompt", reuse_role_output=True)
            r.journal = RunJournal(target, resume_from=source / "checkpoint.json", retry_roles=["bull"])
            def interrupted(_):
                raise RuntimeError("interrupted retry")
            client.responder = interrupted
            with self.assertRaises(RuntimeError):
                r._complete(client, "bull", "same prompt", reuse_role_output=True)
            client.responder = lambda _: dict(claims="new reply")
            r.journal = RunJournal(target, resume_from=source / "checkpoint.json")
            from .agy_session import AntigravityError
            with self.assertRaisesRegex(AntigravityError, "delivery_unconfirmed"):
                r._complete(client, "bull", "same prompt", reuse_role_output=True)
            self.assertEqual(len(client.calls), 2)
            r.journal.retry_roles.add("bull")
            self.assertEqual(r._complete(client, "bull", "same prompt", reuse_role_output=True), dict(claims="new reply"))
            self.assertEqual(len(client.calls), 3)

    def test_cli_hard_exit_replaces_old_delivery_before_cloud_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            save_checkpoint(seeded(), out / "checkpoint.json")
            (out / "result.json").write_text(json.dumps(dict(blocked=False, report_markdown="OLD SUCCESS")))
            code = '''
import os, sys
from unittest.mock import patch
from v4.__main__ import main
from v4.test_integration import CloudDouble
with patch("v4.__main__.AntigravityCloudClient", side_effect=lambda model, **kwargs: CloudDouble(model, lambda _: os._exit(19))):
    main(["--resume", sys.argv[1], "--out", sys.argv[2], "--author-model", "author", "--verifier-model", "first", "--deep-model", "deep", "--global-model", "global"])
'''
            proc = subprocess.run([sys.executable, "-B", "-c", code, str(out / "checkpoint.json"), str(out)],
                                  cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 19, proc.stderr)
            result = json.loads((out / "result.json").read_text())
            self.assertEqual(result, dict(status="running", blocked=True))
            self.assertTrue(load_checkpoint(out / "checkpoint.json").run.claims)

    def test_explicit_role_retry_preserves_old_response_and_other_cached_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            bad = CloudDouble("test/author", lambda _: dict(claims="invalid"))
            good = CloudDouble("test/author", lambda _: dict(claims=[]))
            r._complete(bad, "bull", "bull prompt", reuse_role_output=True)
            r._complete(good, "bear", "bear prompt", reuse_role_output=True)
            r.journal = RunJournal(temp, retry_roles=["bull"])
            retry = CloudDouble("test/author", lambda _: dict(claims=["new response"]))
            self.assertEqual(r._complete(retry, "bull", "bull prompt", reuse_role_output=True), dict(claims=["new response"]))
            self.assertEqual(r._complete(retry, "bear", "bear prompt", reuse_role_output=True), dict(claims=[]))
            self.assertEqual(len(retry.calls), 1)
            archived = [json.loads(p.read_text()) for p in (Path(temp) / "responses" / "archive").glob("*.json")]
            self.assertEqual(len(archived), 3)
            self.assertIn(dict(claims="invalid"), [p["response"] for p in archived])

    def test_received_response_write_failure_does_not_corrupt_checkpoint(self):
        from .checkpoint import write_json
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            r._persist()
            before = (Path(temp) / "checkpoint.json").read_bytes()
            client = CloudDouble("test/model", lambda _: dict(ok=True))
            writes = []
            def fail_received(path, data):
                writes.append(data)
                if data.get("status") == "received":
                    raise OSError("disk full")
                write_json(path, data)
            with patch("v4.journal.write_json", side_effect=fail_received), self.assertRaises(OSError):
                r._complete(client, "task", "prompt")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual((Path(temp) / "checkpoint.json").read_bytes(), before)
            self.assertEqual([w["status"] for w in writes], ["in_flight", "received"])

    def test_trader_checkpoint_failure_rolls_back_trade_and_claims(self):
        r = seeded()
        r.run.claims = [c for c in r.run.claims if c["author_role"] not in {"trader", "risk_manager"}]
        r.run.analyses = {}
        r.run.trade = {}
        r.orch._sync_views(r.run)
        before = copy.deepcopy(r.orch.snapshot(r.run_id))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        r.journal = RunJournal(temp.name)
        c = claim("new.trader")
        c.update(author_role="trader", assertion=dict(kind="trade_plan", trade=dict(direction="WAIT")))
        with patch.object(r.journal, "checkpoint", side_effect=OSError("disk full")), self.assertRaises(OSError):
            r.collect_trader([c], trade=dict(direction="WAIT"))
        self.assertEqual(r.orch.snapshot(r.run_id), before)

    def test_hard_exit_preserves_parallel_response_and_resumes_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "durable-run"
            code = '''
import json, os, sys
from v4.test_integration import seeded, CloudDouble
from v4.test_journal import author_response
from v4.runner import PHASE_2_ROLES
from v4.journal import RunJournal
r = seeded()
r.max_workers = 1
r.run.claims = [c for c in r.run.claims if c["author_role"] in PHASE_2_ROLES]
r.run.analyses = {}
r.run.trade = {}
r.orch._sync_views(r.run)
r.journal = RunJournal(sys.argv[1])
def author(prompt):
    packet = json.loads(prompt.split("<context-data>\\n")[1].split("\\n</context-data>")[0])
    if packet["role"] == "bear":
        os._exit(17)
    return author_response(prompt)
r.resume(CloudDouble("test/author", author), CloudDouble(), CloudDouble("test/deep"), CloudDouble("test/global"))
'''
            proc = subprocess.run([sys.executable, "-B", "-c", code, str(out)],
                                  cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 17, proc.stderr)
            checkpoint = out / "checkpoint.json"
            r = load_checkpoint(checkpoint)
            self.assertEqual({c["author_role"] for c in r.run.claims}, set(PHASE_2_ROLES))
            saved = [json.loads(p.read_text()) for p in (out / "responses").glob("*.json")]
            self.assertEqual({(p["task_id"], p["status"]) for p in saved},
                             {("bull", "received"), ("bear", "in_flight")})
            # Also support resuming into a different permanent directory.
            resumed = Path(temp) / "resumed-run"
            r.journal = RunJournal(resumed, resume_from=checkpoint, retry_roles=["bear"])
            author = CloudDouble("test/author", author_response)
            verifier = CloudDouble()
            result = r.resume(author, verifier, CloudDouble("test/deep"),
                              CloudDouble("test/global", lambda _: global_verdict(r)))
            self.assertFalse(result["blocked"], result)
            roles_called = [json.loads(p.split("<context-data>\n")[1].split("\n</context-data>")[0])["role"]
                            for p in author.calls]
            self.assertEqual(roles_called, ["bear", "research_manager", "trader", "risk_manager"])
            self.assertEqual(len(verifier.calls), len(r.run.claims))
            self.assertTrue((resumed / "checkpoint.json").exists())
            self.assertEqual(load_checkpoint(resumed / "checkpoint.json").receipts, r.receipts)

    def test_failed_parallel_sibling_keeps_each_received_response(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            def respond(prompt):
                if prompt == "bad":
                    raise RuntimeError("network failure")
                return dict(ok=prompt)
            with self.assertRaises(RuntimeError):
                r._parallel(CloudDouble("test/model", respond), [("good", "good"), ("bad", "bad")])
            records = [json.loads(p.read_text()) for p in (Path(temp) / "responses").glob("*.json")]
            self.assertEqual({p["task_id"]: p["status"] for p in records}, {"good": "received", "bad": "in_flight"})

    def test_explicit_new_execution_never_replays_old_review_responses(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            verifier = CloudDouble()
            first = r.execute_phase6(verifier, CloudDouble("test/deep"), CloudDouble("test/author"),
                                     CloudDouble("test/global", lambda _: global_verdict(r)))
            self.assertFalse(first["blocked"], first)
            restored = load_checkpoint(Path(temp) / "checkpoint.json", continue_verification=False)
            restored.journal = RunJournal(temp)
            second = restored.execute_phase6(verifier, CloudDouble("test/deep"), CloudDouble("test/author"),
                                            CloudDouble("test/global", lambda _: global_verdict(restored)))
            self.assertFalse(second["blocked"], second)
            self.assertEqual(len(verifier.calls), 2 * len(r.run.claims))

    def test_four_workers_commit_results_without_eof_and_restore_missing_receipt(self):
        from .test_cloud import cli_stub
        from .cloud import AntigravityCloudClient
        body = """
init()
prompt = json.loads(sys.stdin.readline())["message"]["content"]
task = json.loads(prompt.split("```json\\n", 1)[1].split("\\n```", 1)[0])
checks = [dict(ref_id=ref, relation="SUPPORTS", attributed_assertion="fixture assertion", source_evidence="fixture source", reason="fixture entailment") for ref in task["expected_reference_ids"]]
result(json.dumps(dict(claim_id=task["claim_id"], verdict="PASS", reference_checks=checks, findings=[])))
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            r = seeded(); r.max_workers = 4; r.journal = RunJournal(temp)
            r.journal.checkpoint(r)
            client = AntigravityCloudClient("model", executable=executable, timeout=5)
            prepared = r.prepare_phase6_atomic()
            summary = r._execute_review_batch(client, prepared["atomic_prompts"], r.collect_phase6_atomic)
            self.assertFalse(summary["blocked"], summary)
            self.assertEqual(len(r.receipts), len(r.run.claims))
            # Simulate a crash after a complete session result was fsynced but
            # before its response archive/index or checkpoint receipt committed.
            record_path = next((Path(temp) / "responses").glob("*.json"))
            record = json.loads(record_path.read_text())
            record["status"] = "in_flight"; record.pop("response")
            record_path.write_text(json.dumps(record))
            (Path(temp) / "responses/archive" / (record["attempt_id"] + ".json")).unlink()
            resumed = load_checkpoint(Path(temp) / "checkpoint.json")
            resumed.journal = RunJournal(temp)
            prepared = resumed.prepare_phase6_atomic()
            with patch("v4.agy_session.subprocess.Popen", side_effect=AssertionError("no fresh model turn permitted")) as start:
                summary = resumed._execute_review_batch(client, prepared["atomic_prompts"], resumed.collect_phase6_atomic)
            self.assertFalse(summary["blocked"], summary)
            self.assertEqual(len(resumed.receipts), len(r.run.claims))
            start.assert_not_called()

    def test_disk_failure_stops_before_cloud_work(self):
        r = seeded()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        r.journal = RunJournal(temp.name)
        client = CloudDouble("test/model", lambda _: dict(ok=True))
        with patch("v4.journal.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                r._complete(client, "task", "prompt")
        self.assertEqual(client.calls, [])

    def test_crash_after_inflight_before_spawn_remains_provably_unsent(self):
        from .cloud import AntigravityCloudClient
        from .test_cloud import cli_stub
        with tempfile.TemporaryDirectory() as temp, cli_stub("init()\nsys.stdin.readline()\nresult('{\"ok\":true}')") as executable:
            r = seeded()
            r.journal = RunJournal(temp)
            client = AntigravityCloudClient("model", executable=executable, timeout=3)
            with patch("v4.journal.request_completion", side_effect=SystemExit("crash before spawn")):
                with self.assertRaises(SystemExit):
                    r._complete(client, "bull", "original request", reuse_role_output=True)
            state = json.loads(next((Path(temp) / "sessions").glob("*/session.json")).read_text())
            self.assertFalse(state["request_sent"])
            r.journal = RunJournal(temp)
            self.assertEqual(r._complete(client, "bull", "original request", reuse_role_output=True), dict(ok=True))
            self.assertEqual(len(client.calls), 1)

    def test_exhausted_journal_resumes_after_explicit_same_session_budget_increase(self):
        from .cloud import AntigravityCloudClient
        from .agy_session import AntigravityError
        from .test_recovery_errors import ERROR_BODY, RESUMED_BODY
        from .test_cloud import cli_stub
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing, cli_stub(RESUMED_BODY) as working:
            r = seeded(); r.journal = RunJournal(temp)
            client = AntigravityCloudClient("model", executable=failing, timeout=3, request_attempts=1)
            with self.assertRaisesRegex(AntigravityError, "session_pending"):
                r._complete(client, "bull", "original request", reuse_role_output=True)
            state_path = next((Path(temp) / "sessions").glob("*/session.json"))
            state = json.loads(state_path.read_text()); state["continuations"] = 2
            state_path.write_text(json.dumps(state))
            with self.assertRaisesRegex(AntigravityError, "continuation_exhausted"):
                r._complete(client, "bull", "original request", reuse_role_output=True)
            client = AntigravityCloudClient("model", executable=working, timeout=3,
                                            max_continuations=6, session_budget_seconds=30)
            self.assertEqual(r._complete(client, "bull", "original request", reuse_role_output=True), dict(done=True))
            after = json.loads(state_path.read_text())
            self.assertEqual(after["conversation_id"], state["conversation_id"])
            self.assertEqual(after["continuations"], 3)
            self.assertEqual(after["max_continuations"], 6)
            with patch("v4.agy_session.subprocess.Popen") as start:
                self.assertEqual(r._complete(client, "bull", "original request", reuse_role_output=True), dict(done=True))
            start.assert_not_called()

    def test_request_identity_changes_prevent_role_output_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded()
            r.journal = RunJournal(temp)
            one = CloudDouble("test/one", lambda _: dict(value=1))
            two = CloudDouble("test/two", lambda _: dict(value=2))
            r._complete(one, "author", "original", reuse_role_output=True)
            r._complete(one, "author", "original", reuse_role_output=True)
            r._complete(one, "author", "changed context", reuse_role_output=True)
            r._complete(two, "author", "original", reuse_role_output=True)
            self.assertEqual((len(one.calls), len(two.calls)), (2, 1))


if __name__ == "__main__":
    unittest.main()
