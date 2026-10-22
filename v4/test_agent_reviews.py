"""Native reviews retain accepted siblings and raw reference proofs on restart."""
from pathlib import Path
import json
import tempfile
import unittest
from .agent_reviews import NativeReviewWorkflow
from .checkpoint import save_checkpoint
from .test_integration import seeded, verdict


def response(cid, item, status='PASS'):
    schema = item['schema']
    refs = schema['properties']['reference_checks']['items']['properties']['ref_id'].get('enum', [])
    return verdict(cid, status, refs)


class NativeReviews(unittest.TestCase):
    def test_next_and_submit_match_across_process_hash_seeds(self):
        import os
        import subprocess
        import sys
        from v3.facts import Fact

        script = '''
import json, sys
from v4.agent_reviews import NativeReviewWorkflow
from v4.review_context import calculation_definitions
workflow = NativeReviewWorkflow(sys.argv[2])
payload = json.load(sys.stdin)
result = workflow.next() if sys.argv[1] == 'next' else workflow.submit(**payload)
kinds = ['growth', 'fcf', 'margin', 'return_cc', 'volume_ratio', 'derived_pct',
         'future_a', 'future_b', None]
definitions = calculation_definitions({str(i): {'calc_type': kind} for i, kind in enumerate(kinds)})
print(json.dumps(dict(result=result, serialized_definitions=json.dumps(definitions))))
'''
        with tempfile.TemporaryDirectory() as root:
            runner = seeded()
            for ref, value in [('now', 12), ('prior', 10)]:
                runner.run.fact_store.add(Fact(ref, value, 'USD', 'FY2025', concept='fixture_metric',
                                              source_id='fixture'))
            calcs = [('growth', .2, 'RATIO'), ('fcf', 2, 'USD'), ('margin', 1.2, 'RATIO'),
                     ('return_cc', .2, 'RATIO'), ('volume_ratio', 1.2, 'RATIO'), ('derived_pct', 16.67, 'PCT')]
            for kind, value, unit in calcs:
                runner.run.calc_store.add('calc.' + kind, value, kind,
                    ['prior', 'now'] if kind == 'return_cc' else ['now', 'prior'], unit)
            runner.run.claims[0]['refs'] = ['calc.' + kind for kind, _, _ in calcs]
            runner.run.claims[0]['bindings'] = [dict(ref_id=ref,
                **{dimension: 'unknown' for dimension in ('entity', 'concept', 'period', 'scope', 'unit', 'basis')})
                for ref in runner.run.claims[0]['refs']]
            source = Path(root) / 'source.json'
            save_checkpoint(runner, source)
            workflow = NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')

            def invoke(mode, seed, payload):
                completed = subprocess.run([sys.executable, '-c', script, mode, str(workflow.directory)],
                    input=json.dumps(payload), text=True, capture_output=True, check=True, timeout=20,
                    cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'PYTHONHASHSEED': seed})
                return json.loads(completed.stdout)

            prepared = invoke('next', '1', {})
            packet = prepared['result']
            submitted = invoke('submit', '2', dict(packet_id=packet['packet_id'],
                responses={cid: response(cid, item) for cid, item in packet['items'].items()},
                executor='host/cross-process', input_hash=packet['input_hash']))
            self.assertFalse(submitted['result'].get('blocked'), submitted['result'])
            self.assertEqual(prepared['serialized_definitions'], submitted['serialized_definitions'])

    def test_next_reuses_prepared_tasks_for_review_packet_context(self):
        from unittest.mock import patch
        import v4.runner as runner_module

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'original.json'
            save_checkpoint(seeded(), source)
            workflow = NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')

            original_builder = runner_module.build_atomic_tasks
            build_count = []

            def counted_builder(run):
                build_count.append(None)
                return original_builder(run)

            with patch('v4.runner.build_atomic_tasks', side_effect=counted_builder):
                packet = workflow.next()

            self.assertEqual(packet.get('kind'), 'atomic', packet)
            self.assertEqual(packet.get('status'), 'reviews', packet)
            self.assertLessEqual(len(build_count), 4, build_count)

            build_count.clear()
            with patch('v4.runner.build_atomic_tasks', side_effect=counted_builder):
                resumed_packet = workflow.next()
            self.assertEqual(resumed_packet.get('packet_id'), packet['packet_id'])
            self.assertLessEqual(len(build_count), 4, build_count)

            with workflow.queue.transaction() as state:
                reference = workflow._load(state)
            tasks = reference._tasks()
            prepared = reference._prepare_phase6_atomic(tasks, enforce_budget=False)
            prompts = dict(prepared['atomic_prompts'])
            for claim_id, item in packet['items'].items():
                self.assertEqual(item['prompt'], prompts[claim_id])
                self.assertEqual(item['schema'], prepared['response_schemas'][claim_id])
                self.assertEqual(item['review_context'],
                                 reference.review_context_for('atomic', [claim_id], tasks=tasks))

    def test_older_global_receipt_cannot_hide_new_deterministic_failure_or_block_diagnostics(self):
        from unittest.mock import patch
        from v3.facts import Fact
        with tempfile.TemporaryDirectory() as root:
            r=seeded()
            for fid,value in [('now',2),('before',1)]:
                r.run.fact_store.add(Fact(fid,value,'USD','FY2030',concept=fid,source_id='fixture'))
            r.run.calc_store.add('old.rounded',1.00004,'growth',['now','before'],'RATIO')
            source=Path(root)/'legacy.json';save_checkpoint(r,source)
            w=NativeReviewWorkflow.from_checkpoint(source,Path(root)/'native')
            # Simulate a receipt accepted by the former global 1e-4 policy.
            with patch('v4.pipeline.calculation_findings',return_value=[]):
                packet=self._reach_global(w)
                w.submit(packet['packet_id'],{next(iter(packet['items'])):self._global_response(w)},
                         'host/global',packet['input_hash'])
                self.assertEqual(w.next()['status'],'complete')
            current=NativeReviewWorkflow(w.directory).next()
            self.assertEqual(current['status'],'blocked:deterministic',current)
            self.assertFalse(current['delivery_ready'])
            self.assertTrue(any(f['object_id']=='old.rounded' for f in current['findings']))

    def test_legacy_invalid_event_routes_to_patch_instead_of_global_hard_wall(self):
        with tempfile.TemporaryDirectory() as root:
            r = seeded()
            from v3.facts import Fact
            r.run.fact_store.add(Fact('source.event', 5, 'USD', 'FY2026', concept='quantity', source_id='fixture'))
            r.run.claims[0].update(refs=['source.event'],bindings=[dict(ref_id='source.event',
                **{k:'unknown' for k in ('entity','concept','period','scope','unit','basis')})],
                assertion=dict(kind='event',event_ref='source.event',is_aggregate=True))
            source = Path(root) / 'legacy-event.json'; save_checkpoint(r, source)
            w = NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')
            for _ in range(20):
                packet = w.next()
                if packet.get('kind') != 'atomic':
                    break
                w.submit(packet['packet_id'],{cid:response(cid,item) for cid,item in packet['items'].items()},
                         'host/test',packet['input_hash'])
            self.assertEqual(packet.get('kind'),'patch',packet)
            self.assertEqual(packet['allowed_claim_ids'],['a'])
            outcome = w.submit_patch(packet['packet_id'],[dict(claim_id='a',assertion=dict(kind='text'))],
                                     'host/repair',packet['input_hash'])
            self.assertFalse(outcome.get('blocked'),outcome)
            packet = NativeReviewWorkflow(w.directory).next()
            self.assertEqual(packet['kind'],'atomic')

    def test_native_event_registration_is_transactional_and_survives_restart(self):
        from .evidence_repair import register_event_catalog
        with tempfile.TemporaryDirectory() as root:
            w = self.bootstrap(root)
            with w.queue.transaction() as state:
                r=w._load(state)
                from v3.facts import Fact
                r.run.fact_store.add(Fact('source.quantity',5,'USD','FY2026',concept='quantity',source_id='fixture'))
                w._save(state,r)
                ref='source.quantity'
            event=dict(event_id='catalog.event',date='2026-10-15',source_ref=ref,
                entity='TEST',concept='quantity',period='FY2026',unit='USD',basis='reported',
                scope='consolidated',value=5,is_aggregate=False)
            changed=register_event_catalog(w,[event],reason='main Agent registered structured evidence')
            self.assertEqual(changed['status'],'events_registered')
            with w.queue.transaction() as state:
                r=w._load(state)
                self.assertEqual(r.run.events['catalog.event'],event)
                r.run.claims[0].update(refs=['catalog.event'],assertion=dict(kind='event',event_ref='catalog.event',is_aggregate=False),
                    bindings=[dict(ref_id='catalog.event',**{k:'unknown' for k in ('entity','concept','period','scope','unit','basis')})])
                w._save(state,r)
                before=state['input_hash']
            with self.assertRaisesRegex(ValueError,'cannot remove referenced events'):
                register_event_catalog(w,[],reason='must patch Claim before catalog removal')
            with self.assertRaises(ValueError):
                register_event_catalog(w,[{**event,'value':float('nan')}],reason='invalid candidate')
            with w.queue.transaction() as state:
                self.assertEqual(state['input_hash'],before)

    def test_oversized_first_claim_does_not_starve_siblings_and_budget_is_resumable(self):
        from .agent_reviews import configure_review_budget
        from v3.evidence import EvidencePacket
        with tempfile.TemporaryDirectory() as root:
            r = seeded()
            claim_a = next(c for c in r.run.claims if c['claim_id'] == 'independent')
            evidence_id = 'evidence.large-review-input'
            r.run.evidence_store.add(EvidencePacket(
                evidence_id=evidence_id, claim_candidate='x' * (70 * 1024),
                source_id='fixture.large', source_type='news_json', source_span='fixture span'))
            claim_a['refs'].append(evidence_id)
            claim_a['bindings'].append(dict(ref_id=evidence_id,
                **{name: 'unknown' for name in ('entity','concept','period','scope','unit','basis')}))
            source = Path(root) / 'large.json'; save_checkpoint(r, source)
            w = NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')
            accepted = []
            for _ in range(20):
                packet = NativeReviewWorkflow(w.directory).next()
                if packet.get('blocked'):
                    break
                self.assertEqual(packet['kind'], 'atomic', packet)
                self.assertNotIn('independent', packet['items'])
                accepted.extend(packet['items'])
                w.submit(packet['packet_id'], {cid: response(cid, item) for cid,item in packet['items'].items()},
                         'host/test', packet['input_hash'])
            self.assertTrue(accepted)
            self.assertEqual(packet['status'], 'blocked:review_context_budget')
            self.assertIn('independent', [x['claim_id'] for x in packet['oversized_items']])
            blocked_item = next(row for row in packet['oversized_items'] if row['claim_id'] == 'independent')
            self.assertGreater(blocked_item['own_evidence_bytes'], 70 * 1024)
            self.assertEqual(blocked_item['wire_bytes'], blocked_item['own_evidence_bytes'] +
                             blocked_item['shared_context_bytes'] + blocked_item['schema_bytes'])
            changed = configure_review_budget(w, max_item_bytes=128 * 1024, max_packet_bytes=700000,
                                             reason='operator approved larger review context')
            self.assertEqual(changed['status'], 'review_budget_updated')
            with w.queue.transaction() as state:
                configured = w._load(state)
                self.assertEqual(configured.review_batch_max_bytes, 128 * 1024)
                self.assertEqual(configured.review_deep_batch_max_bytes, 128 * 1024)
            next_packet = NativeReviewWorkflow(w.directory).next()
            self.assertIn('independent', next_packet['items'])
            self.assertFalse(set(accepted).intersection(next_packet['items']))
            w.submit(next_packet['packet_id'], {'independent': response('independent',
                     next_packet['items']['independent'], 'SUSPICIOUS')},
                     'host/test', next_packet['input_hash'])
            deep_packet = NativeReviewWorkflow(w.directory).next()
            self.assertEqual(deep_packet.get('kind'), 'deep', deep_packet)
            self.assertIn('independent', deep_packet['items'])

    def test_cli_review_budget_flags_are_available_without_changing_claim_hash(self):
        from .agent_cli import main
        with tempfile.TemporaryDirectory() as root:
            w = self.bootstrap(root)
            with w.queue.transaction() as state:
                before = state['input_hash']
            self.assertEqual(main(['configure-reviews','--work-dir',str(w.directory),
                '--review-batch-max-bytes','131072','--review-packet-max-bytes','262144',
                '--reason','approved context budget']),0)
            with w.queue.transaction() as state:
                self.assertEqual(state['input_hash'],before)
                self.assertEqual(w._load(state).review_batch_max_bytes,131072)
                self.assertTrue(state['review_budget_changes'])
    def bootstrap(self, root, extra=False):
        runner = seeded()
        if extra:
            next(c for c in runner.run.claims if c['claim_id'] == 'b')['dependencies'].append('independent')
        source = Path(root) / 'original.json'; save_checkpoint(runner, source)
        return NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')

    def test_financial_contract_upgrade_retires_old_packet_and_reissues_review(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        previous_contract = 'financial-evidence/4'
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'legacy.json'
            work_dir = Path(root) / 'native'
            with ExitStack() as stack:
                for module in ('runner', 'atomic_phase6', 'agent_workflow', 'checkpoint'):
                    stack.enter_context(patch(f'v4.{module}.FINANCIAL_REVIEW_VERSION', previous_contract))
                save_checkpoint(seeded(), source)
                legacy = NativeReviewWorkflow.from_checkpoint(source, work_dir)
                old_packet = legacy.next()

            self.assertEqual(old_packet['kind'], 'atomic', old_packet)
            resumed = NativeReviewWorkflow(work_dir)
            current_packet = resumed.next()
            self.assertEqual(current_packet['kind'], 'atomic', current_packet)
            self.assertNotEqual(current_packet['packet_id'], old_packet['packet_id'])
            self.assertEqual(set(current_packet['items']), set(old_packet['items']))
            self.assertNotEqual(current_packet['input_hash'], old_packet['input_hash'])

            with resumed.queue.transaction() as state:
                upgrade = state['contract_upgrades'][-1]
                self.assertEqual(upgrade['previous_contract'], previous_contract)
                self.assertEqual(upgrade['current_contract'], 'financial-evidence/9')
                self.assertTrue(state['packets'][old_packet['packet_id']]['retired'])
                self.assertEqual(state['packets'][old_packet['packet_id']]['retirement_reason'],
                                 'financial_review_contract_upgraded')
                self.assertEqual(state['financial_review_contract'], 'financial-evidence/9')

    def test_nonpass_reviews_issue_patch_then_revalidate_descendants(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            for _ in range(12):
                packet = workflow.next()
                if packet.get('kind') not in {'atomic', 'deep'}:
                    break
                workflow.submit(packet['packet_id'], {cid: response(cid, item, 'FAIL' if cid == 'b' else 'PASS')
                    for cid, item in packet['items'].items()}, 'host/test', packet['input_hash'])
            self.assertEqual(packet.get('kind'), 'patch', packet)
            self.assertEqual(packet['allowed_claim_ids'], ['b'])
            resumed = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(resumed['packet_id'], packet['packet_id'])
            outcome = workflow.submit_patch(packet['packet_id'], [{'claim_id': 'b', 'text': 'Corrected premise'}],
                                            'host/author', packet['input_hash'])
            self.assertFalse(outcome.get('blocked'), outcome)
            next_packet = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(set(next_packet['items']), {'b', 'c'})

    def test_imported_pending_patch_without_prompt_is_reconstructed_at_limit(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root); self._reach_global(workflow)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state); runner.max_revisions = 0
                runner.prepare_phase6_revision_v4([dict(claim_id='a', issue='wrong premise',
                    evidence='fixture', suggested_fix='revise')])
                workflow._save(state, runner)
            imported = NativeReviewWorkflow.from_checkpoint(workflow.checkpoint_path, Path(root) / 'imported')
            packet = imported.next()
            self.assertEqual(packet.get('kind'), 'patch', packet)
            self.assertEqual(packet['allowed_claim_ids'], ['a'])
            self.assertTrue(packet['manual_patch_only'])
            self.assertTrue(packet['blocked'])
            self.assertEqual(packet['status'], 'blocked:revision_limit')
            self.assertIn('wrong premise', next(iter(packet['items'].values()))['prompt'])
            with self.assertRaisesRegex(ValueError,'explicit submit_patch'):
                imported.submit(packet['packet_id'], {'claim_patcher':[{'claim_id':'a','text':'changed'}]},
                                'host/automatic', packet['input_hash'])
            with imported.queue.transaction() as state:
                self.assertEqual(workflow._load(state).revision_round, 0)

    def test_partial_delivery_restarts_without_rescheduling_accepted_siblings(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root); owners = {}; seen = []
            while True:
                packet = workflow.next()
                if packet['kind'] == 'global':
                    break
                self.assertEqual(packet['kind'], 'atomic')
                cid, item = next(iter(packet['items'].items())); seen.append(cid)
                owner = 'host/' + str(len(seen) % 2); owners[cid] = owner
                outcome = workflow.submit(packet['packet_id'], {cid: response(cid, item)}, owner, packet['input_hash'])
                self.assertFalse(outcome['blocked'], outcome)
                workflow = NativeReviewWorkflow(workflow.directory)
                following = workflow.next()
                if len(packet['items']) > 1:
                    self.assertEqual(following['packet_id'], packet['packet_id'])
                    self.assertNotIn(cid, following['items'])
            self.assertEqual(len(seen), len(set(seen)))
            self.assertEqual(len(seen), 12)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual({c: r['model'] for c, r in runner.receipts.items()}, owners)
                self.assertEqual(runner._contract_attempts, {})
                with self.assertRaises(ValueError):
                    runner.finalize()

    def test_only_nonpass_goes_deep_same_model_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root); deep = []
            while True:
                packet = workflow.next()
                if packet['kind'] == 'global':
                    break
                outputs = {cid: response(cid, item, 'FAIL' if cid == 'b' and packet['kind'] == 'atomic' else 'PASS')
                           for cid, item in packet['items'].items()}
                if packet['kind'] == 'deep':
                    deep.extend(outputs)
                outcome = workflow.submit(packet['packet_id'], outputs, 'host/same', packet['input_hash'])
                self.assertFalse(outcome['blocked'], outcome)
            self.assertEqual(deep, ['b'])
            self.assertEqual(packet['kind'], 'global')

    def test_reference_supplement_keeps_other_pending_items_and_restores_composition(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root, extra=True); packet = workflow.next()
            self.assertIn('b', packet['items'])
            partial = verdict('b', ref_ids=['a'])
            outcome = workflow.submit(packet['packet_id'], {'b': partial}, 'host/one', packet['input_hash'])
            self.assertTrue(outcome['needs_retry'])
            following = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(following['packet_id'], packet['packet_id'])
            self.assertNotIn('b', following['items'])
            workflow.submit(packet['packet_id'], {cid: response(cid, item) for cid, item in following['items'].items()},
                            'host/one', packet['input_hash'])
            seen_supplement = False
            while True:
                packet = NativeReviewWorkflow(workflow.directory).next()
                if packet['kind'] == 'global':
                    break
                if 'b' in packet['items']:
                    self.assertEqual([c['ref_id'] for c in response('b', packet['items']['b'])['reference_checks']], ['independent'])
                    seen_supplement = True
                outcome = workflow.submit(packet['packet_id'], {cid: response(cid, item) for cid, item in packet['items'].items()},
                                          'host/one', packet['input_hash'])
                self.assertFalse(outcome.get('blocked'), outcome)
            self.assertTrue(seen_supplement)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual(len(runner.receipts), 12)
                self.assertEqual(len(runner.receipts['b']['result']['reference_checks']), 2)

    def test_raw_candidate_saved_before_crash_is_collected_on_next(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root); packet = workflow.next(); cid, item = next(iter(packet['items'].items()))
            workflow.queue.submit(packet['packet_id'], {cid: response(cid, item)}, 'host/main', packet['input_hash'])
            following = NativeReviewWorkflow(workflow.directory).next()
            self.assertNotIn(cid, following['items'])
            with workflow.queue.transaction() as state:
                self.assertIn(cid, workflow._load(state).receipts)


    def _reach_global(self, workflow):
        for _ in range(32):
            packet = workflow.next()
            if packet.get('kind') == 'global':
                return packet
            self.assertIn(packet.get('kind'), ('atomic', 'deep'), packet)
            outputs = {cid: response(cid, item) for cid, item in packet['items'].items()}
            outcome = workflow.submit(packet['packet_id'], outputs, 'host/same', packet['input_hash'])
            self.assertFalse(outcome.get('blocked'), outcome)
        self.fail('P6 global reviewer was never issued')

    def _global_response(self, workflow):
        with workflow.queue.transaction() as state:
            runner = workflow._load(state)
            claims = {c['claim_id']: c for c in runner.run.claims}
            target = runner.pending_global['target_claim_ids']
        return dict(findings=[], resolved_external_findings=[], dependency_reviews=[
            dict(claim_id=cid, required_dependencies=claims[cid]['dependencies'],
                 unresolved=False, reason='verified against canonical dependency graph')
            for cid in target])

    def test_global_packet_reloads_and_finalizes_through_real_collector(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            packet = self._reach_global(workflow)
            reopened = NativeReviewWorkflow(workflow.directory)
            same = reopened.next()
            self.assertEqual(same['packet_id'], packet['packet_id'])
            self.assertEqual(same['items'], packet['items'])
            role = next(iter(packet['items']))
            outcome = reopened.submit(packet['packet_id'],
                                      {role: self._global_response(reopened)},
                                      'host/same', packet['input_hash'])
            self.assertFalse(outcome.get('blocked'), outcome)
            self.assertEqual(outcome['status'], 'verified')
            complete = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(complete['status'], 'complete')
            self.assertFalse(complete['blocked'])
            again = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(again['status'], 'complete')
            self.assertEqual(again['report_markdown'], complete['report_markdown'])
            self.assertEqual(again['sidecar']['report_hash'], complete['sidecar']['report_hash'])

    def test_invalid_global_response_is_fail_closed_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            packet = self._reach_global(workflow)
            role = next(iter(packet['items']))
            outcome = workflow.submit(packet['packet_id'], {role: {'not_a_review': True}},
                                      'host/same', packet['input_hash'])
            self.assertTrue(outcome['blocked'], outcome)
            self.assertEqual(outcome['status'], 'blocked:global_contract')
            reopened = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(reopened['status'], 'blocked:global_contract')
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertIsNone(runner.global_receipt)
                with self.assertRaises(ValueError):
                    runner.finalize()

    def test_missing_global_raw_archive_prevents_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            packet = self._reach_global(workflow)
            role = next(iter(packet['items']))
            outcome = workflow.submit(packet['packet_id'],
                                      {role: self._global_response(workflow)},
                                      'host/same', packet['input_hash'])
            self.assertEqual(outcome['status'], 'verified')
            import json
            archive = workflow.directory / 'responses' / 'archive'
            files = [path for path in archive.glob('*.json')
                     if json.loads(path.read_text()).get('task_id') == role]
            self.assertEqual(len(files), 1)
            files[0].unlink()
            with self.assertRaisesRegex(ValueError, 'verified raw response archive'):
                NativeReviewWorkflow(workflow.directory).next()


    def test_global_three_batch_replay_survives_every_restart(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.global_dependency_batch_size = 4
                workflow._save(state, runner)
            packet = self._reach_global(workflow)
            roles = []
            for index in range(3):
                role = next(iter(packet['items']))
                roles.append(role)
                result = workflow.submit(packet['packet_id'],
                                         {role: self._global_response(workflow)},
                                         'host/same', packet['input_hash'])
                self.assertEqual(result['status'], ('verified' if index == 2 else
                    'blocked:global_dependency_coverage'), result)
                workflow = NativeReviewWorkflow(workflow.directory)
                following = workflow.next()
                if index < 2:
                    self.assertEqual(following['kind'], 'global')
                    self.assertNotEqual(following['packet_id'], packet['packet_id'])
                    packet = following
                else:
                    self.assertEqual(following['status'], 'complete')
            self.assertEqual(roles, ['global_consistency_reviewer',
                                     'global_dependency_completion',
                                     'global_dependency_completion'])
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['status'], 'complete')

    def test_received_global_candidate_is_collected_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            packet = self._reach_global(workflow)
            role = next(iter(packet['items']))
            workflow.queue.submit(packet['packet_id'],
                                  {role: self._global_response(workflow)},
                                  'host/same', packet['input_hash'])
            resumed = NativeReviewWorkflow(workflow.directory)
            outcome = resumed.next()
            self.assertEqual(outcome['status'], 'verified')
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['status'], 'complete')

    def test_global_dependency_reviewer_identity_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.global_dependency_batch_size = 4
                workflow._save(state, runner)
            packet = self._reach_global(workflow)
            role = next(iter(packet['items']))
            first = workflow.submit(packet['packet_id'],
                                    {role: self._global_response(workflow)},
                                    'host/first', packet['input_hash'])
            self.assertTrue(first['needs_retry'])
            workflow = NativeReviewWorkflow(workflow.directory)
            packet = workflow.next()
            self.assertEqual(next(iter(packet['items'])), 'global_dependency_completion')
            wrong = workflow.submit(packet['packet_id'],
                                    {next(iter(packet['items'])): self._global_response(workflow)},
                                    'host/other', packet['input_hash'])
            self.assertEqual(wrong['status'], 'blocked:global_contract')
            self.assertTrue(wrong['blocked'])
            resumed = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(resumed['status'], 'blocked:global_contract')


    def test_global_rejection_can_explicitly_retry_without_losing_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            bad = self._reach_global(workflow)
            role = next(iter(bad['items']))
            failed = workflow.submit(bad['packet_id'], {role: {'not_a_review': True}},
                                     'host/same', bad['input_hash'])
            self.assertEqual(failed['status'], 'blocked:global_contract')
            resumed = NativeReviewWorkflow(workflow.directory)
            with self.assertRaises(ValueError):
                resumed.retry_global(' ')
            with self.assertRaises(ValueError):
                resumed.retry_global('')
            self.assertEqual(resumed.next()['status'], 'blocked:global_contract')
            retried = resumed.retry_global('invalid JSON fields; request a corrected answer')
            self.assertEqual(retried['status'], 'retry_global')
            fresh = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(fresh['kind'], 'global')
            self.assertNotEqual(fresh['packet_id'], bad['packet_id'])
            with self.assertRaises(ValueError):
                workflow.submit(bad['packet_id'], {role: self._global_response(workflow)},
                                'host/same', bad['input_hash'])
            corrected = workflow.submit(fresh['packet_id'], {role: self._global_response(workflow)},
                                        'host/same', fresh['input_hash'])
            self.assertEqual(corrected['status'], 'verified')
            complete = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(complete['status'], 'complete')
            with workflow.queue.transaction() as state:
                self.assertEqual(len(state['global_retry_history']), 1)
                self.assertEqual(state['global_retry_history'][0]['packet_id'], bad['packet_id'])
                self.assertEqual(len(workflow._load(state).receipts), 12)


    def test_one_claim_global_batches_survive_twelve_restarts(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.global_dependency_batch_size = 1
                workflow._save(state, runner)
            packet = self._reach_global(workflow)
            for round_index in range(12):
                role = next(iter(packet['items']))
                answer = self._global_response(workflow)
                outcome = workflow.submit(packet['packet_id'], {role: answer},
                                          'host/same', packet['input_hash'])
                workflow = NativeReviewWorkflow(workflow.directory)
                following = workflow.next()
                if round_index < 11:
                    self.assertEqual(outcome['status'], 'blocked:global_dependency_coverage')
                    self.assertEqual(following['kind'], 'global')
                    packet = following
                else:
                    self.assertEqual(outcome['status'], 'verified')
                    self.assertEqual(following['status'], 'complete')

    def test_global_outcome_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            packet = self._reach_global(workflow)
            role = next(iter(packet['items']))
            workflow.submit(packet['packet_id'], {role: self._global_response(workflow)},
                            'host/same', packet['input_hash'])
            import json
            state = json.loads(workflow.queue.state_path.read_text())
            state['global_outcomes'][packet['packet_id']]['status'] = 'fake_verified'
            workflow.queue.state_path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'replayed global outcome differs'):
                NativeReviewWorkflow(workflow.directory).next()


class NativeGlobalFindingsPatch(NativeReviews):
    """P6 global material findings route through the canonical Claim patch repair."""

    def _dependency_gap_sweep(self, workflow, batch_size=4):
        with workflow.queue.transaction() as state:
            runner = workflow._load(state)
            runner.global_dependency_batch_size = batch_size
            workflow._save(state, runner)
        packet = self._reach_global(workflow)
        for _ in range(12):
            role = next(iter(packet['items']))
            answer = self._global_response(workflow)
            for row in answer['dependency_reviews']:
                if row['claim_id'] == 'a':
                    row['required_dependencies'] = ['independent']
            outcome = workflow.submit(packet['packet_id'], {role: answer},
                                      'host/same', packet['input_hash'])
            if outcome['status'] == 'revision':
                return outcome
            self.assertEqual(outcome['status'], 'blocked:global_dependency_coverage', outcome)
            packet = NativeReviewWorkflow(workflow.directory).next()
        self.fail('dependency audit did not close')

    def test_completed_dependency_gap_never_reissues_last_batch_and_patch_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            self._dependency_gap_sweep(workflow)
            patch = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(patch.get('kind'), 'patch', patch)
            for _ in range(3):
                resumed = NativeReviewWorkflow(workflow.directory)
                self.assertEqual(resumed.next()['packet_id'], patch['packet_id'])
                with resumed.queue.transaction() as state:
                    runner = resumed._load(state)
                    self.assertIsNone(runner.pending_global)
                    self.assertIsNone(runner.global_review_progress)
                    self.assertIsNotNone(runner.pending_patch)
                    with self.assertRaisesRegex(ValueError, 'pending claim repairs'):
                        runner.prepare_phase6_global_sweep()
            outcome = resumed.submit_patch(patch['packet_id'],
                [dict(claim_id='a', dependencies=['independent'])], 'host/patcher', patch['input_hash'])
            self.assertFalse(outcome.get('blocked'), outcome)
            self.assertEqual(set(outcome['revalidate_claim_ids']), {'a','b','c'})
            seen = []
            for _ in range(20):
                step = NativeReviewWorkflow(workflow.directory).next()
                seen.append(step.get('kind') or step['status'])
                if step['status'] == 'complete':
                    break
                if step['kind'] == 'global':
                    output = {next(iter(step['items'])): self._global_response(workflow)}
                else:
                    self.assertEqual(step['kind'], 'atomic')
                    output = {cid: response(cid, item) for cid,item in step['items'].items()}
                resumed.submit(step['packet_id'], output, 'host/same', step['input_hash'])
            else:
                self.fail('dependency patch never reached a new clean global sweep')
            self.assertIn('atomic', seen)
            self.assertIn('global', seen)
            self.assertEqual(seen[-1], 'complete')

    def test_pending_patch_does_not_block_historical_global_replay(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            self._dependency_gap_sweep(workflow)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.prepare_phase6_revision_v4(runner.global_receipt['findings'])
                workflow._save(state, runner)
            resumed = NativeReviewWorkflow(workflow.directory)
            patch = resumed.next()
            self.assertEqual(patch.get('kind'), 'patch', patch)
            self.assertEqual(patch['allowed_claim_ids'], ['a'])

    def test_legacy_reissued_completion_packet_is_retired_after_verified_revision_close(self):
        from copy import deepcopy
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            self._dependency_gap_sweep(workflow)
            with workflow.queue.transaction() as state:
                last = [p for p in state['packets'].values() if p['kind'] == 'global'][-1]
                duplicate = deepcopy(last)
                duplicate.update(packet_id=uuid4().hex, received={}, retired=False)
                state['packets'][duplicate['packet_id']] = duplicate
                state['active_packet_id'] = duplicate['packet_id']
            resumed = NativeReviewWorkflow(workflow.directory)
            patch = resumed.next()
            self.assertEqual(patch.get('kind'), 'patch', patch)
            with workflow.queue.transaction() as state:
                self.assertTrue(state['packets'][duplicate['packet_id']]['retired'])
                self.assertEqual(state['packets'][duplicate['packet_id']]['retirement_reason'],
                                 'global_round_already_closed_with_findings')

    def test_pending_repair_still_requires_all_original_global_archives(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            self._dependency_gap_sweep(workflow)
            patch = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(patch['kind'], 'patch')
            for path in (workflow.directory / 'responses' / 'archive').glob('*.json'):
                if json.loads(path.read_text()).get('task_id') == 'global_dependency_completion':
                    path.unlink()
                    break
            else:
                self.fail('missing completion archive fixture')
            with self.assertRaisesRegex(ValueError, 'verified raw response archive'):
                NativeReviewWorkflow(workflow.directory).next()

    def _finding(self, runner, cid='a'):
        deps = next(c for c in runner.run.claims if c['claim_id'] == cid)['dependencies']
        return dict(claim_id=cid, category='semantic_conflict', issue='Fixture global conflict for test claim ' + cid,
                    evidence=dict(trigger='synthetic global sweep'),
                    suggested_fix='repair the fixture claim text')

    def _global_with_findings(self, workflow, finding):
        packet = self._reach_global(workflow)
        role = next(iter(packet['items']))
        answer = self._global_response(workflow)
        answer['findings'] = [finding]
        return workflow.submit(packet['packet_id'], {role: answer}, 'host/same', packet['input_hash'])

    def test_epoch_reset_retires_accepted_links_and_preserves_findings_for_new_review(self):
        from copy import deepcopy
        from uuid import uuid4
        for legacy in (False, True):
            with self.subTest(legacy_link=legacy), tempfile.TemporaryDirectory() as root:
                workflow = self.bootstrap(root)
                finding = dict(unresolved=True, issue='Unmapped fixture claim', evidence='fixture',
                               suggested_fix='Find the actual target and repair it.')
                self._global_with_findings(workflow, finding)
                link = workflow.next()
                result = workflow.submit(link['packet_id'], {'finding_linker': [dict(
                    finding_index=0, claim_ids=['a'], unresolved=False, reason='Target is a.')]},
                    'host/linker', link['input_hash'])
                self.assertEqual(result['status'], 'linked', result)
                with workflow.queue.transaction() as state:
                    runner = workflow._load(state)
                    if legacy:
                        state['accepted_links'].pop('verification_epoch', None)
                        runner = workflow._load(state)
                        self.assertIsNotNone(runner.global_receipt)
                    original_links = deepcopy(state['accepted_links'])
                    runner._verification_epoch = uuid4().hex
                    workflow._save(state, runner)
                resumed = NativeReviewWorkflow(workflow.directory)
                self.assertEqual(resumed.next()['kind'], 'atomic')
                with resumed.queue.transaction() as state:
                    self.assertNotIn('accepted_links', state)
                    self.assertEqual(state['historical_links'][-1], original_links)
                    runner = resumed._load(state)
                    self.assertTrue({f['finding_id'] for f in original_links['findings']} <=
                                    {f['finding_id'] for f in runner.deferred_global_findings})
                new_global = self._reach_global(resumed)
                response = self._global_response(resumed)
                response['findings'] = [finding]
                resumed.submit(new_global['packet_id'], {next(iter(new_global['items'])): response},
                               'host/global', new_global['input_hash'])
                new_link = resumed.next()
                self.assertEqual(new_link['kind'], 'link', new_link)
                self.assertNotEqual(new_link['packet_id'], link['packet_id'])

    def test_unrouted_global_finding_links_then_patches_then_revalidates(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            finding = dict(unresolved=True, category='semantic_conflict',
                           issue='Fixture unknown Claim target',
                           evidence=dict(trigger='synthetic global sweep'),
                           suggested_fix='repair the semantic mismatch')
            outcome = self._global_with_findings(workflow, finding)
            self.assertTrue(outcome['needs_revision'], outcome)
            link = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(link['kind'], 'link')
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['packet_id'],
                             link['packet_id'])
            result = NativeReviewWorkflow(workflow.directory).submit(
                link['packet_id'], {'finding_linker': [
                    dict(finding_index=0, claim_ids=['a'], unresolved=False,
                         reason='linked to the original Claim a')]},
                'host/linker', link['input_hash'])
            self.assertEqual(result['status'], 'linked', result)
            patch = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(patch['kind'], 'patch', patch)
            self.assertIn('a', patch['allowed_claim_ids'])
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['packet_id'],
                             patch['packet_id'])
            fixed = NativeReviewWorkflow(workflow.directory).submit_patch(
                patch['packet_id'], [dict(claim_id='a', text='Corrected semantic analysis')],
                'host/patcher', patch['input_hash'])
            self.assertFalse(fixed.get('blocked'), fixed)
            seen = []
            for _ in range(20):
                step = NativeReviewWorkflow(workflow.directory).next()
                seen.append(step.get('kind') or step.get('status'))
                if step.get('status') == 'complete':
                    self.assertTrue(step['report_markdown'])
                    break
                if step.get('kind') == 'global':
                    output = {next(iter(step['items'])): self._global_response(workflow)}
                else:
                    output = {cid: response(cid, item) for cid, item in step['items'].items()}
                done = NativeReviewWorkflow(workflow.directory).submit(
                    step['packet_id'], output, 'host/review', step['input_hash'])
                self.assertFalse(done.get('blocked') and not done.get('needs_retry'), done)
            else:
                self.fail(f'linking and patching did not complete: {seen}')
            self.assertIn('atomic', seen)
            self.assertIn('global', seen)

    def test_linker_invalid_mapping_requires_explicit_retry(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            finding = dict(unresolved=True, category='semantic_conflict',
                           issue='Fixture unresolved semantic finding',
                           evidence=dict(trigger='synthetic'), suggested_fix='link to a Claim')
            self._global_with_findings(workflow, finding)
            link = NativeReviewWorkflow(workflow.directory).next()
            bad = NativeReviewWorkflow(workflow.directory).submit(
                link['packet_id'], {'finding_linker': [
                    dict(finding_index=0, claim_ids=['unknown'], unresolved=False,
                         reason='invalid synthetic mapping')]},
                'host/linker', link['input_hash'])
            self.assertEqual(bad['status'], 'blocked:link_contract', bad)
            after = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(after['status'], 'blocked:link_contract')
            with self.assertRaises(ValueError):
                NativeReviewWorkflow(workflow.directory).retry_link('')
            repaired = NativeReviewWorkflow(workflow.directory).retry_link('correct invalid mapping')
            self.assertEqual(repaired['status'], 'retry_link')
            fresh = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(fresh['kind'], 'link')
            self.assertNotEqual(fresh['packet_id'], link['packet_id'])
    def test_global_finding_opens_patch_then_revalidates_to_new_global(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                finding = self._finding(runner)
            outcome = self._global_with_findings(workflow, finding)
            self.assertTrue(outcome['blocked'] and outcome['needs_revision'])
            repaired = []
            kinds = []
            for _ in range(40):
                packet = NativeReviewWorkflow(workflow.directory).next()
                kinds.append(packet.get('kind') or packet.get('status'))
                if packet.get('status') == 'complete':
                    break
                if packet.get('kind') == 'patch':
                    with workflow.queue.transaction() as state:
                        runner = workflow._load(state)
                        before_ids = set(runner.receipts)
                    self.assertIn('a', packet['allowed_claim_ids'])
                    patch = [dict(claim_id='a', text='Repaired after global finding ' + finding['issue'][:24])]
                    result = NativeReviewWorkflow(workflow.directory).submit_patch(
                        packet['packet_id'], patch, 'host/patcher', packet['input_hash'])
                    self.assertFalse(result.get('blocked'), result)
                    self.assertIn('a', result['revalidate_claim_ids'])
                    with workflow.queue.transaction() as state:
                        runner = workflow._load(state)
                        self.assertIsNone(runner.global_receipt)
                        self.assertEqual(runner.pending_patch, None)
                        self.assertEqual(runner.revision_round, 1)
                        self.assertEqual(len(runner.history), 1)
                        self.assertTrue(set(runner.receipts) < before_ids)
                    continue
                if packet.get('kind') == 'global':
                    role = next(iter(packet['items']))
                    output = {role: self._global_response(NativeReviewWorkflow(workflow.directory))}
                else:
                    outputs = {cid: response(cid, item) for cid, item in packet['items'].items()}
                    output = outputs
                result = NativeReviewWorkflow(workflow.directory).submit(
                    packet['packet_id'], output, 'host/same', packet['input_hash'])
                self.assertFalse(result.get('blocked') and not result.get('needs_retry'), result)
            else:
                self.fail('patched pipeline never reached a new P6 state')
            self.assertIn('patch', kinds)
            self.assertIn('atomic', kinds[kinds.index('patch'):])
            self.assertIn('global', kinds[kinds.index('patch'):])
            self.assertEqual(kinds[-1], 'complete')
            repaired.append(True)

    def test_patch_packet_survives_restart_and_rejects_stale_or_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                finding = self._finding(runner)
            self._global_with_findings(workflow, finding)
            packet = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(packet['kind'], 'patch')
            again = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(again['packet_id'], packet['packet_id'])
            self.assertEqual(again['items'], packet['items'])
            with self.assertRaises(ValueError):
                NativeReviewWorkflow(workflow.directory).submit_patch(
                    packet['packet_id'], [dict(claim_id='a', text='x')], 'host/x', 'stale-hash')
            with self.assertRaises(ValueError):
                NativeReviewWorkflow(workflow.directory).submit_patch(
                    'no-such-packet', [dict(claim_id='a', text='x')], 'host/x', packet['input_hash'])
            with self.assertRaises(ValueError):
                NativeReviewWorkflow(workflow.directory).submit_patch(
                    packet['packet_id'], [], 'host/x', packet['input_hash'])
            with self.assertRaises(ValueError):
                NativeReviewWorkflow(workflow.directory).submit_patch(
                    packet['packet_id'], [dict(text='no claim id')], 'host/x', packet['input_hash'])

    def test_noop_patch_keeps_findings_open_without_false_pass(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                finding = self._finding(runner)
                original_text = next(c for c in runner.run.claims if c['claim_id'] == 'a')['text']
            self._global_with_findings(workflow, finding)
            packet = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(packet['kind'], 'patch')
            result = NativeReviewWorkflow(workflow.directory).submit_patch(
                packet['packet_id'], [dict(claim_id='a', text=original_text)],
                'host/patcher', packet['input_hash'])
            self.assertTrue(result['blocked'])
            self.assertEqual(result['status'], 'blocked:revision_no_changes')
            resumed = NativeReviewWorkflow(workflow.directory)
            state = json.loads(resumed.queue.state_path.read_text())
            self.assertEqual(state['checkpoint']['revision_round'], 0)
            next_packet = resumed.next()
            self.assertEqual(next_packet['kind'], 'patch')
            self.assertNotEqual(next_packet['packet_id'], packet['packet_id'])

    def test_unknown_claim_patch_fails_closed_and_operator_can_retry(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                finding = self._finding(runner)
            self._global_with_findings(workflow, finding)
            packet = NativeReviewWorkflow(workflow.directory).next()
            failed = NativeReviewWorkflow(workflow.directory).submit_patch(
                packet['packet_id'], [dict(claim_id='nonexistent', text='x')],
                'host/patcher', packet['input_hash'])
            self.assertEqual(failed['status'], 'blocked:patch_contract')
            self.assertEqual(failed['next_action'], 'retry-patch')
            self.assertEqual(failed['failed_packet_id'], packet['packet_id'])
            resumed = NativeReviewWorkflow(workflow.directory)
            stopped = resumed.next()
            self.assertEqual(stopped['status'], 'blocked:patch_contract')
            self.assertEqual(stopped['next_action'], 'retry-patch')
            with self.assertRaises(ValueError):
                resumed.retry_patch('  ')
            retried = resumed.retry_patch('patch targeted an unknown claim id; request a corrected patch')
            self.assertEqual(retried['status'], 'retry_patch')
            fresh = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(fresh['kind'], 'patch')
            self.assertNotEqual(fresh['packet_id'], packet['packet_id'])
            with workflow.queue.transaction() as state:
                self.assertEqual(len(state['patch_retry_history']), 1)

    def test_non_patchable_route_emits_evidence_work_without_patch_packet(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            finding = dict(claim_id='a', category='evidence_gap',
                           issue='Fixture requires new evidence outside the current sources',
                           evidence=dict(trigger='synthetic'),
                           resolution_route='evidence_needed',
                           resolution_reason='current evidence cannot settle the conflict',
                           evidence_request=dict(proposition='Obtain the missing source.'))
            outcome = self._global_with_findings(workflow, finding)
            self.assertTrue(outcome['blocked'])
            work = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(work['status'], 'evidence_recovery')
            self.assertFalse(work['blocked'])
            self.assertFalse(work['delivery_ready'])
            self.assertEqual(work['evidence_requests'][0]['claim_ids'], ['a'])

    def test_deep_evidence_finding_reaches_native_work_and_search_is_durable(self):
        import contextlib
        import io
        from .agent_cli import main
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            for _ in range(24):
                packet = NativeReviewWorkflow(workflow.directory).next()
                if packet.get('status') == 'evidence_recovery':
                    break
                self.assertIn(packet.get('kind'), ('atomic', 'deep'), packet)
                outputs = {}
                for cid, item in packet['items'].items():
                    status = 'SUSPICIOUS' if packet['kind'] == 'atomic' and cid == 'a' else 'PASS'
                    if packet['kind'] == 'deep' and cid == 'a':
                        status = 'FAIL'
                    answer = response(cid, item, status)
                    if cid == 'a' and status != 'PASS':
                        answer['findings'] = [dict(issue='The source does not establish this period.',
                            resolution_route='evidence_needed',
                            resolution_reason='The current source does not cover the requested period.',
                            evidence_request=dict(proposition='Find source evidence for this period.'))]
                    outputs[cid] = answer
                workflow.submit(packet['packet_id'], outputs, 'host/main', packet['input_hash'])
            else:
                self.fail('accepted deep evidence finding did not reach recovery work')

            self.assertEqual(packet['status'], 'evidence_recovery')
            self.assertFalse(packet['delivery_ready'])
            request = packet['evidence_requests'][0]
            self.assertEqual(request['resolution_status'], 'unresolved')
            resumed = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(resumed['status'], 'evidence_recovery')
            self.assertEqual(resumed['evidence_requests'][0]['request_id'], request['request_id'])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['record-evidence-search', '--work-dir', str(workflow.directory),
                    '--request-id', request['request_id'], '--outcome', 'not_found',
                    '--query', 'official filing', '--details', 'No exact passage located'])
            self.assertEqual(code, 0)
            recorded = json.loads(output.getvalue())
            self.assertEqual(recorded['status'], 'evidence_search_recorded')
            again = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(again['evidence_requests'][0]['resolution_status'], 'unresolved')
            self.assertEqual(again['evidence_requests'][0]['search_results'][-1]['outcome'], 'not_found')


if __name__ == '__main__':
    unittest.main()
