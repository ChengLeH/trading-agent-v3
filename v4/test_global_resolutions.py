"""Global evidence resolutions require complete, state-bound target proofs."""
import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from .agent_reviews import NativeReviewWorkflow
from .checkpoint import save_checkpoint
from .test_integration import review_all, seeded, verdict


class GlobalEvidenceResolutions(unittest.TestCase):
    @staticmethod
    def _bootstrap(root):
        source = Path(root) / "original.json"
        save_checkpoint(seeded(), source)
        return NativeReviewWorkflow.from_checkpoint(source, Path(root) / "native")

    @staticmethod
    def _reach_global(workflow):
        for _ in range(32):
            packet = workflow.next()
            if packet.get("kind") == "global":
                return packet
            if packet.get("kind") not in {"atomic", "deep"}:
                raise AssertionError(f"unexpected workflow packet: {packet}")
            answers = {}
            for cid, item in packet["items"].items():
                ref_ids = item["schema"]["properties"]["reference_checks"]["items"][
                    "properties"]["ref_id"].get("enum", [])
                answers[cid] = verdict(cid, ref_ids=ref_ids)
            outcome = workflow.submit(packet["packet_id"], answers, "host/global", packet["input_hash"])
            if outcome.get("blocked"):
                raise AssertionError(f"review unexpectedly blocked: {outcome}")
        raise AssertionError("global reviewer packet was never issued")

    @staticmethod
    def _native_global_response(workflow):
        with workflow.queue.transaction() as state:
            runner = workflow._load(state)
            claims = {claim["claim_id"]: claim for claim in runner.run.claims}
            target_ids = runner.pending_global["target_claim_ids"]
        return dict(findings=[], resolved_external_findings=[], dependency_reviews=[
            dict(claim_id=cid, required_dependencies=claims[cid]["dependencies"],
                 unresolved=False, reason="verified against canonical dependency graph")
            for cid in target_ids])

    @staticmethod
    def _global_review(runner, findings=None):
        return dict(findings=findings or [], dependency_reviews=[
            dict(claim_id=claim["claim_id"], required_dependencies=claim["dependencies"],
                 unresolved=False, reason="The declared dependency graph was reviewed.")
            for claim in runner.run.claims])

    def _finding(self, claim_ids):
        return dict(
            claim_ids=claim_ids,
            issue="The cited evidence does not support the full proposition.",
            evidence="The available source omits a material premise.",
            suggested_fix="Add evidence or narrow the Claim to what its evidence supports.",
            materiality="material",
            resolution_route="evidence_needed",
            resolution_reason="The original proposition exceeds current evidence.",
            evidence_request=dict(proposition="Find a source that supports the proposition."),
        )

    def _first_global(self, runner, finding):
        runner.prepare_phase6_global_sweep()
        result = runner.collect_phase6_global_sweep(
            self._global_review(runner, [finding]), model_identity="test/global")
        self.assertTrue(result["blocked"], result)
        return runner.deferred_global_findings[0]["finding_id"]

    def _revise_and_recheck_ancestor(self, runner, claim_id="a"):
        runner.prepare_phase6_revision_v4([dict(
            claim_id=claim_id, issue="Revise a declared causal premise",
            evidence="A valid local patch is explicitly requested by this fixture.",
            suggested_fix="Update the premise and revalidate its descendants.")])
        runner.collect_phase6_revision([dict(
            claim_id=claim_id, text="Revised premise, with the same Claim identity.")])
        review_all(runner)

    def _claim_revised_row(self, finding_id, targets, reviewed):
        return dict(
            finding_id=finding_id,
            resolution_mode="claim_revised",
            evidence="The revised premise narrows what the original evidence is asked to establish.",
            reason="The global reviewer checked each target against its current evidence and receipts.",
            claim_ids=targets,
            resolution_refs=[],
            target_reviews=reviewed,
        )

    def _completed_resolution_workflow(self, root):
        workflow = self._bootstrap(root)
        packet = self._reach_global(workflow)
        response = self._native_global_response(workflow)
        response['findings'] = [self._finding(['b'])]
        workflow.submit(packet['packet_id'], {next(iter(packet['items'])): response},
                        'host/global', packet['input_hash'])
        with workflow.queue.transaction() as state:
            runner = workflow._load(state)
            finding_id = runner.deferred_global_findings[0]['finding_id']
            runner.prepare_phase6_revision_v4([dict(claim_id='b', issue='Narrow the proposition',
                evidence='Current source supports only a narrower statement.', suggested_fix='Revise b.')])
            runner.collect_phase6_revision([dict(claim_id='b', text='The supported narrower proposition.')])
            workflow._save(state, runner)
        packet = self._reach_global(workflow)
        response = self._native_global_response(workflow)
        response['resolved_external_findings'] = [self._claim_revised_row(finding_id, ['b'], [
            dict(claim_id='b', status='resolved', reason='The revised target is supported.')])]
        result = workflow.submit(packet['packet_id'], {next(iter(packet['items'])): response},
                                 'host/global', packet['input_hash'])
        self.assertEqual(result['status'], 'verified', result)
        return workflow, packet['packet_id']

    def test_old_epoch_packet_without_received_row_does_not_poison_current_recovery(self):
        from copy import deepcopy
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as root:
            workflow, final_id = self._completed_resolution_workflow(root)
            with workflow.queue.transaction() as state:
                historical = deepcopy(state['packets'][final_id])
                historical['packet_id'] = uuid4().hex
                historical['global_context']['verification_epoch'] = uuid4().hex
                historical['received'] = {}
                state['packets'][historical['packet_id']] = historical
                state['global_outcomes'][historical['packet_id']] = deepcopy(state['global_outcomes'][final_id])
            for _ in range(2):
                self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['status'], 'complete')

    def test_current_epoch_missing_received_row_or_archive_still_requires_raw_proof(self):
        for missing in ('received', 'archive'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as root:
                workflow, final_id = self._completed_resolution_workflow(root)
                with workflow.queue.transaction() as state:
                    packet = state['packets'][final_id]
                    role = next(iter(packet['items']))
                    response = packet['received'][role]['response']
                    if missing == 'received':
                        packet['received'] = {}
                if missing == 'archive':
                    paths = [path for path in (workflow.directory / 'responses' / 'archive').glob('*.json')
                             if json.loads(path.read_text())['response'] == response]
                    self.assertTrue(paths)
                    for path in paths:
                        path.unlink()
                with self.assertRaisesRegex(ValueError, f'{final_id}.*verified raw response archive'):
                    NativeReviewWorkflow(workflow.directory).next()

    def test_new_epoch_requires_new_global_instead_of_restoring_old_resolution(self):
        from uuid import uuid4
        for active in (False, True):
            with self.subTest(old_packet_active=active), tempfile.TemporaryDirectory() as root:
                workflow, final_id = self._completed_resolution_workflow(root)
                with workflow.queue.transaction() as state:
                    runner = workflow._load(state)
                    runner._verification_epoch = uuid4().hex
                    workflow._save(state, runner)
                    if active:
                        state['packets'][final_id]['retired'] = False
                        state['active_packet_id'] = final_id
                packet = self._reach_global(NativeReviewWorkflow(workflow.directory))
                self.assertEqual(packet['kind'], 'global')
                self.assertNotEqual(packet['packet_id'], final_id)
                if active:
                    with workflow.queue.transaction() as state:
                        self.assertEqual(state['packets'][final_id]['retirement_reason'],
                                         'verification_epoch_changed')

    def test_unanswered_legacy_global_prompt_is_reissued_without_losing_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self._bootstrap(root)
            original = self._reach_global(workflow)
            with workflow.queue.transaction() as state:
                role = next(iter(state['packets'][original['packet_id']]['items']))
                state['packets'][original['packet_id']]['items'][role]['prompt'] = 'Legacy global instructions'
                receipts = deepcopy(workflow._load(state).receipts)
            packet = NativeReviewWorkflow(workflow.directory).next()
            self.assertNotEqual(packet['packet_id'], original['packet_id'])
            self.assertIn('existing_evidence', packet['items'][role]['prompt'])
            with workflow.queue.transaction() as state:
                self.assertTrue(state['packets'][original['packet_id']]['retired'])
                self.assertEqual(workflow._load(state).receipts, receipts)
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['packet_id'], packet['packet_id'])
            response = self._native_global_response(workflow)
            result = workflow.submit(packet['packet_id'], {role: response}, 'host/test', packet['input_hash'])
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['status'], 'complete')

    def test_existing_evidence_requires_new_links_and_fresh_support_for_every_target(self):
        from v3.facts import Fact
        from .schema import digest
        for case in ('valid', 'unknown_ref', 'not_in_baseline', 'unbound', 'unchanged',
                     'stale_receipt', 'unsupported', 'partial_targets', 'duplicate_refs', 'missing_deep'):
            with self.subTest(case=case):
                runner = seeded()
                runner.run.fact_store.add(Fact('available.amount', 42, 'USD', 'FY2025',
                    entity='TEST', concept='Revenue', basis='gaap', scope='consolidated', source_id='fixture'))
                runner.run.fact_store.add(Fact('available.unbound', 99, 'USD', 'FY2024',
                    entity='TEST', concept='Revenue', basis='gaap', scope='consolidated', source_id='fixture'))
                review_all(runner)
                finding_id = self._first_global(runner, self._finding(['a', 'independent']))
                original = deepcopy(runner.deferred_global_findings)
                runner.prepare_phase6_revision_v4([dict(claim_id=cid, issue='Missing citation',
                    evidence='The source already exists.', suggested_fix='Link the canonical fact.')
                    for cid in ('a', 'independent')])
                runner.collect_phase6_revision([dict(claim_id=cid, refs=['available.amount'],
                    bindings=[dict(ref_id='available.amount', entity='TEST', concept='Revenue',
                        period='FY2025', scope='consolidated', unit='USD', basis='gaap')])
                    for cid in ('a', 'independent')])
                review_all(runner)
                proof = dict(finding_id=finding_id, resolution_mode='existing_evidence',
                    claim_ids=['a', 'independent'], resolution_refs=['available.amount'],
                    evidence='The canonical fact supports each repaired target.', reason='Semantic review completed.')
                if case == 'unknown_ref':
                    proof['resolution_refs'] = ['missing.source']
                elif case == 'not_in_baseline':
                    original[0]['resolution_baseline']['source_ids'] = []
                elif case == 'unbound':
                    proof['resolution_refs'] = ['available.unbound']
                elif case == 'unchanged':
                    current = next(c for c in runner.run.claims if c['claim_id'] == 'a')
                    original[0]['resolution_baseline']['claim_hashes']['a'] = digest(current)
                elif case == 'stale_receipt':
                    runner.receipts['a']['input_hash'] = 'stale'
                elif case == 'unsupported':
                    runner.receipts['a']['result']['reference_checks'][0]['relation'] = 'INSUFFICIENT'
                elif case == 'partial_targets':
                    proof['claim_ids'] = ['a']
                elif case == 'duplicate_refs':
                    proof['resolution_refs'] *= 2
                elif case == 'missing_deep':
                    runner.receipts['a']['result']['verdict'] = 'SUSPICIOUS'
                accepted, rejected, _ = runner._validate_external_resolutions(
                    [proof], original, model_identity='test/global')
                self.assertEqual(bool(accepted), case == 'valid', (accepted, rejected))
                self.assertEqual(bool(rejected), case != 'valid', (accepted, rejected))

    def test_claim_revised_requires_all_targets_and_relevant_history(self):
        runner = seeded()
        review_all(runner)
        finding_id = self._first_global(runner, self._finding(["b", "c"]))
        self._revise_and_recheck_ancestor(runner)

        runner.prepare_phase6_global_sweep()
        partial_targets = dict(findings=[], resolved_external_findings=[
            self._claim_revised_row(finding_id, ["b"], [
                dict(claim_id="b", status="resolved", reason="Current evidence now supports the narrower statement."),
                dict(claim_id="c", status="resolved", reason="Current evidence now supports the narrower statement."),
            ])])
        result = runner.collect_phase6_global_sweep(
            self._global_review(runner) | partial_targets, model_identity="test/global")

        self.assertTrue(result["blocked"], result)
        self.assertTrue(result["rejected_external_resolutions"])
        self.assertEqual(runner.deferred_global_findings[0]["finding_id"], finding_id)

    def test_changed_target_without_recorded_relevant_history_does_not_resolve(self):
        runner = seeded()
        review_all(runner)
        finding_id = self._first_global(runner, self._finding(["b"]))
        claim = next(row for row in runner.run.claims if row["claim_id"] == "b")
        claim["text"] = "A changed sentence without a revision-history record."
        review_all(runner)

        runner.prepare_phase6_global_sweep()
        response = self._global_review(runner) | {"resolved_external_findings": [
            self._claim_revised_row(finding_id, ["b"], [
                dict(claim_id="b", status="resolved", reason="The narrower statement is now supported."),
            ])]}
        result = runner.collect_phase6_global_sweep(response, model_identity="test/global")

        self.assertTrue(result["blocked"], result)
        self.assertTrue(result["rejected_external_resolutions"])
        self.assertEqual(runner.deferred_global_findings[0]["finding_id"], finding_id)

    def test_unresolved_target_proof_survives_checkpoint_round_trip(self):
        from .checkpoint import load_checkpoint, save_checkpoint

        runner = seeded()
        review_all(runner)
        finding_id = self._first_global(runner, self._finding(["b"]))
        self._revise_and_recheck_ancestor(runner, "b")
        runner.prepare_phase6_global_sweep()
        response = self._global_review(runner) | {"resolved_external_findings": [
            self._claim_revised_row(finding_id, ["b"], [dict(
                claim_id="b", status="unresolved",
                reason="The current evidence still does not support the original proposition.")])]
        }
        result = runner.collect_phase6_global_sweep(response, model_identity="test/global")
        self.assertEqual(result["status"], "revision")
        self.assertIn("resolution_progress", runner.deferred_global_findings[0])

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "checkpoint.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
        self.assertEqual(restored.deferred_global_findings[0]["resolution_progress"],
                         runner.deferred_global_findings[0]["resolution_progress"])

    def test_partial_target_proof_resumes_with_only_missing_target(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self._bootstrap(root)
            packet = self._reach_global(workflow)
            finding = self._finding(["b", "c"])
            role = next(iter(packet["items"]))
            first = self._native_global_response(workflow)
            first["findings"] = [finding]
            workflow.submit(packet["packet_id"], {role: first}, "host/global", packet["input_hash"])

            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.prepare_phase6_revision_v4([dict(
                    claim_id="a", issue="Revise the declared premise",
                    evidence="The fixture requests a premise correction.",
                    suggested_fix="Update this premise and revalidate descendants.")])
                runner.collect_phase6_revision([dict(
                    claim_id="a", text="Revised causal premise for both target Claims.")])
                workflow._save(state, runner)

            packet = self._reach_global(NativeReviewWorkflow(workflow.directory))
            role = next(iter(packet["items"]))
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                finding_id = next(row["finding_id"] for row in runner.deferred_global_findings
                                  if row.get("resolution_route") == "evidence_needed")
            partial = self._native_global_response(workflow)
            partial["resolved_external_findings"] = [self._claim_revised_row(
                finding_id, ["b", "c"], [dict(
                    claim_id="b", status="resolved",
                    reason="The current evidence supports the narrower target statement.")])]
            outcome = workflow.submit(packet["packet_id"], {role: partial},
                                      "host/global", packet["input_hash"])
            self.assertTrue(outcome["needs_resolution_completion"], outcome)

            completion = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(completion["kind"], "global")
            self.assertEqual(next(iter(completion["items"])), "global_dependency_completion")
            payload = json.loads(completion["items"]["global_dependency_completion"]["prompt"].split(
                "<context-data>\n", 1)[1].split("\n</context-data>", 1)[0])
            self.assertEqual(payload["target_claim_ids"], ["c"])
            self.assertEqual(payload["review_scope"], "external_resolution_completion")

            restarted = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(restarted["packet_id"], completion["packet_id"])
            self.assertEqual(restarted["items"], completion["items"])
            completion_role = next(iter(completion["items"]))
            answer = dict(findings=[], dependency_reviews=[], resolved_external_findings=[
                self._claim_revised_row(finding_id, ["b", "c"], [dict(
                    claim_id="c", status="resolved",
                    reason="The current evidence supports the narrower target statement.")])])
            completed = restarted and NativeReviewWorkflow(workflow.directory).submit(
                completion["packet_id"], {completion_role: answer},
                "host/global", completion["input_hash"])
            self.assertEqual(completed["status"], "verified", completed)
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()["status"], "complete")

    def test_exhausted_target_completion_does_not_issue_another_packet(self):
        runner = seeded()
        review_all(runner)
        runner.max_contract_attempts = 1
        finding_id = self._first_global(runner, self._finding(["b", "c"]))
        self._revise_and_recheck_ancestor(runner)
        runner.prepare_phase6_global_sweep()
        partial = self._global_review(runner) | {"resolved_external_findings": [
            self._claim_revised_row(finding_id, ["b", "c"], [dict(
                claim_id="b", status="resolved", reason="The revised premise supports this target.")])]
        }
        result = runner.collect_phase6_global_sweep(partial, model_identity="test/global")
        self.assertFalse(result["needs_retry"])
        exhausted = runner.prepare_phase6_global_sweep()
        self.assertIsInstance(exhausted, dict)
        self.assertEqual(exhausted["status"], "blocked:global_resolution_coverage")
        self.assertFalse(exhausted["needs_retry"])


if __name__ == "__main__":
    unittest.main()
