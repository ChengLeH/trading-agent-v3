"""Real persisted Native queue, actual batch archives, no paid models."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .agent_reviews import NativeReviewWorkflow
from .agent_dispatch import dispatch_secondary_reviews
from .test_agent_reviews import NativeReviews, response
from .test_integration import verdict


def batch_answer(request, omit=()):
    schema = request['response_schema']
    if 'results' not in schema.get('properties', {}):
        cid = request['claim_ids'][0]
        return response(cid, dict(schema=schema))
    schemas = schema['properties']['results']['items']['anyOf']
    return dict(results=[response(s['properties']['claim_id']['enum'][0], dict(schema=s))
        for s in schemas if s['properties']['claim_id']['enum'][0] not in omit])


class Scheduling(unittest.TestCase):
    def test_single_request_reserves_only_its_own_items(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = NativeReviews().bootstrap(root)
            first = workflow.claim_reviews('agy/first', max_items=4, batch_size=1, single_request=True)
            self.assertEqual(len(first['requests']), 1)
            self.assertEqual(first['lease']['item_ids'], first['requests'][0]['claim_ids'])
            second = NativeReviewWorkflow(workflow.directory).claim_reviews(
                'host/second', max_items=4, batch_size=1, single_request=True)
            self.assertEqual(len(second['requests']), 1)
            self.assertFalse(set(first['lease']['item_ids']) & set(second['lease']['item_ids']))

    def test_unstarted_expired_request_is_reclaimed_without_old_owner_restart(self):
        from .agent_queue import renew_items_in_state
        with tempfile.TemporaryDirectory() as root:
            workflow = NativeReviews().bootstrap(root)
            with patch('v4.agent_queue.time.time', return_value=100.0):
                first = workflow.claim_reviews('agy/first', max_items=1, batch_size=1,
                                               single_request=True, lease_seconds=5)
            with patch('v4.agent_queue.time.time', return_value=106.0):
                second = NativeReviewWorkflow(workflow.directory).claim_reviews(
                    'host/second', max_items=1, batch_size=1, single_request=True)
                self.assertEqual(first['lease']['item_ids'], second['lease']['item_ids'])
                with workflow.queue.transaction() as state:
                    renewed = renew_items_in_state(state, first['packet_id'], 'agy/first',
                        first['lease']['lease_id'], 3600)
                self.assertEqual(renewed['item_ids'], [])

    def test_disjoint_owners_restart_and_batch_raw_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            a = w.claim_reviews('agy/reviewer', max_items=4, batch_size=4)
            b = NativeReviewWorkflow(w.directory).claim_reviews('host/main', max_items=4, batch_size=4)
            self.assertEqual(a['status'], 'reviews_claimed')
            self.assertEqual(b['status'], 'reviews_claimed')
            self.assertFalse(set(a['lease']['item_ids']) & set(b['lease']['item_ids']))
            self.assertLess(len(a['requests']), len(a['lease']['item_ids']))
            for claimed, identity in [(b, 'host/main'), (a, 'agy/reviewer')]:
                for req in claimed['requests']:
                    result = NativeReviewWorkflow(w.directory).submit_review_request(claimed['packet_id'],
                        req['request_id'], batch_answer(req), identity, claimed['input_hash'])
                    self.assertFalse(result.get('blocked'), result)
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertEqual(len(r.receipts), 8)
                self.assertEqual({r.receipts[c]['model'] for c in a['lease']['item_ids']}, {'agy/reviewer'})
                self.assertEqual({r.receipts[c]['model'] for c in b['lease']['item_ids']}, {'host/main'})
                source = r._latest_review_sources['atomic'][a['lease']['item_ids'][0]]
                self.assertIn('attempt_id', source)
            # Reload revalidates raw batched responses rather than trusting
            # the packet's per-row summary or a fabricated single prompt.
            with NativeReviewWorkflow(w.directory).queue.transaction() as state:
                r = w._load(state)
                self.assertEqual(len(r.receipts), 8)

    def test_missing_batch_row_keeps_siblings_and_only_missing_is_reissued(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            claimed = w.claim_reviews('host/main', max_items=4, batch_size=4)
            req = claimed['requests'][0]
            missing = req['claim_ids'][0]
            result = w.submit_review_request(claimed['packet_id'], req['request_id'],
                batch_answer(req, omit=[missing]), 'host/main', claimed['input_hash'])
            self.assertEqual(result['omitted_claim_ids'], [missing])
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertNotIn(missing, r.receipts)
                self.assertTrue(set(req['claim_ids'][1:]) <= set(r.receipts))
            pending = w.next()
            self.assertIn(missing, pending['items'])
            self.assertFalse(set(req['claim_ids'][1:]) & set(pending['items']))

    def test_owner_cannot_use_old_wire_after_lease_reclaim(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            with patch('v4.agent_queue.time.time', return_value=100):
                claimed = w.claim_reviews('agy/reviewer', max_items=2, batch_size=2, lease_seconds=10)
            with patch('v4.agent_queue.time.time', return_value=120):
                w.queue.claim_items(claimed['packet_id'], 'host/main', claimed['lease']['item_ids'])
                with self.assertRaisesRegex(ValueError, 'lease'):
                    req = claimed['requests'][0]
                    w.submit_review_request(claimed['packet_id'], req['request_id'], batch_answer(req),
                        'agy/reviewer', claimed['input_hash'])

    def test_optimized_dispatch_uses_fewer_calls_and_crosses_packet_boundary(self):
        class BatchClient:
            identity = 'agy/fake-batches'
            def __init__(self): self.calls = []
            def complete_structured(self, prompt, schema):
                self.calls.append(prompt)
                self_schema = schema['properties']['results']['items']['anyOf']
                return dict(results=[response(s['properties']['claim_id']['enum'][0], dict(schema=s))
                                     for s in self_schema])
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            client = BatchClient()
            result = dispatch_secondary_reviews(w, client, max_workers=2, max_items=16,
                                                batch_size=4, continuous=True)
            self.assertFalse(result['failures'], result)
            self.assertGreater(len(result['accepted_claim_ids']), 8)
            self.assertLess(len(client.calls), len(result['accepted_claim_ids']))
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertEqual(set(result['accepted_claim_ids']), set(r.receipts))
                self.assertTrue(all(not p.get('item_leases') for p in state['packets'].values()))

    def test_byte_split_leases_only_started_worker_requests(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from .review_batches import pack_review_requests as pack
        release = threading.Event()
        class SplitClient:
            identity = 'agy/split'
            def complete_structured(self, prompt, schema):
                rows = schema['properties']['results']['items']['anyOf']
                cid = rows[0]['properties']['claim_id']['enum'][0]
                if cid != 'a':
                    if not release.wait(10):
                        raise RuntimeError('test observer did not release workers')
                return dict(results=[response(cid, dict(schema=rows[0]))])
        def split(tasks, **kwargs):
            return pack(tasks, **{**kwargs, 'batch_size': 1})
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            with patch('v4.native_scheduling.pack_review_requests', side_effect=split):
                with ThreadPoolExecutor(max_workers=1) as host:
                    future = host.submit(dispatch_secondary_reviews, w, SplitClient(),
                                         max_workers=2, max_items=4, batch_size=4)
                    try:
                        deadline = time.monotonic() + 8
                        while True:
                            with w.queue.transaction() as state:
                                packet = next((p for p in state['packets'].values()
                                    if 'a' in p.get('received', {})), None)
                                if packet:
                                    still_owned = set(packet.get('item_leases', {}))
                                    break
                            if time.monotonic() > deadline:
                                self.fail('first split response was not collected')
                            time.sleep(.01)
                        self.assertIn('b', still_owned)
                        self.assertLessEqual(len(still_owned), 2)
                        main = NativeReviewWorkflow(w.directory).claim_reviews('host/main', max_items=4)
                        self.assertFalse(set(main['lease']['item_ids']) & still_owned)
                        w.queue.release_items(main['packet_id'], 'host/main', main['lease']['lease_id'])
                    finally:
                        release.set()
                    outcome = future.result(timeout=10)
                    self.assertFalse(outcome['failures'], outcome)
                    self.assertEqual(len(outcome['accepted_claim_ids']), 4)

    def test_quota_handoff_restarts_only_incomplete_claim_without_mixing_reviewers(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            packet = w.next()
            answers = {cid: response(cid, item) for cid, item in packet['items'].items()}
            answers['b']['reference_checks'] = []
            w.submit(packet['packet_id'], answers, 'agy/old', packet['input_hash'])
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertIn('b', r.reference_supplements['atomic'])
                self.assertIn('a', r.receipts)
                self.assertNotIn('b', r.receipts)
                state.setdefault('secondary_availability', {})['agy/old'] = dict(
                    status='unavailable', reason_code='quota_exhausted')
            claimed = NativeReviewWorkflow(w.directory).claim_reviews('host/main', max_items=4, batch_size=4)
            self.assertIn('b', claimed['lease']['item_ids'])
            for req in claimed['requests']:
                result = w.submit_review_request(claimed['packet_id'], req['request_id'],
                    batch_answer(req), 'host/main', claimed['input_hash'])
                self.assertFalse(result.get('blocked'), result)
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertEqual(r.receipts['a']['model'], 'agy/old')
                self.assertEqual(r.receipts['b']['model'], 'host/main')
                self.assertEqual(state['review_handoffs'][0]['claim_id'], 'b')
                self.assertNotIn('b', r.reference_supplements['atomic'])
                self.assertNotIn('reference_parent_sources',
                    claimed['requests'][0]['review_context'])

    def test_explicit_takeover_of_unavailable_or_relinquished_partial_review(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            packet = w.next()
            answers = {cid: response(cid, item) for cid, item in packet['items'].items()}
            answers['b']['reference_checks'] = []
            w.submit(packet['packet_id'], answers, 'host/old', packet['input_hash'])
            claimed = w.claim_reviews('host/new', max_items=4, takeover_incomplete=True)
            self.assertIn('b', claimed['lease']['item_ids'])
            for req in claimed['requests']:
                w.submit_review_request(claimed['packet_id'], req['request_id'], batch_answer(req),
                                        'host/new', claimed['input_hash'])
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertEqual(r.receipts['a']['model'], 'host/old')
                self.assertEqual(r.receipts['b']['model'], 'host/new')
                self.assertTrue(state['review_handoffs'][0]['reason'].startswith('explicit_reviewer_takeover'))

    def test_identical_wire_redelivery_after_reload_is_idempotent_and_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            claimed = w.claim_reviews('host/main', max_items=4, batch_size=4)
            req = claimed['requests'][0]
            answer = batch_answer(req)
            result = w.submit_review_request(claimed['packet_id'], req['request_id'], answer,
                                            'host/main', claimed['input_hash'])
            repeated = NativeReviewWorkflow(w.directory).submit_review_request(
                claimed['packet_id'], req['request_id'], answer, 'host/main', claimed['input_hash'])
            self.assertTrue(repeated.pop('idempotent'))
            self.assertEqual(repeated, result)
            with w.queue.transaction() as state:
                self.assertEqual(len(w._load(state).receipts), len(req['claim_ids']))
            changed = json.loads(json.dumps(answer))
            changed['results'][0]['verdict'] = 'FAIL'
            with self.assertRaisesRegex(ValueError, 'conflicting duplicate'):
                w.submit_review_request(claimed['packet_id'], req['request_id'], changed,
                                        'host/main', claimed['input_hash'])

    def test_abandoned_atomic_packet_is_cold_stored_after_deep_overlap(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            first = w.claim_reviews('agy/atomic', max_items=8, batch_size=1)
            request = next(req for req in first['requests'] if req['claim_ids'] == ['a'])
            suspicious = response('a', dict(schema=request['response_schema']), status='SUSPICIOUS')
            w.submit_review_request(first['packet_id'], request['request_id'], suspicious,
                                    'agy/atomic', first['input_hash'])
            other = w.claim_reviews('host/atomic', max_items=4, batch_size=1)
            deep = w.claim_reviews('host/deep', max_items=1, batch_size=1)
            self.assertEqual(deep['kind'], 'deep')
            w.queue.release_items(first['packet_id'], 'agy/atomic', first['lease']['lease_id'])
            pending = NativeReviewWorkflow(w.directory).next()
            self.assertEqual(pending['kind'], 'atomic')
            with w.queue.transaction() as state:
                self.assertTrue(state['packets'][first['packet_id']]['retired'])
                self.assertTrue(state['packets'][deep['packet_id']]['item_leases'])
                self.assertTrue(state['packets'][other['packet_id']]['item_leases'])
                self.assertIn('a', w._load(state).receipts)
            raw = json.loads(w.queue.state_path.read_text())
            self.assertIn('$native_archive', raw['packets'][first['packet_id']]['items'])

    def test_duplicate_and_unknown_batch_rows_preserve_only_unambiguous_siblings(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            claimed = w.claim_reviews('host/main', max_items=4, batch_size=4)
            req = claimed['requests'][0]
            answer = batch_answer(req)
            duplicate = answer['results'][0]
            answer['results'].append(dict(duplicate))
            answer['results'].append(verdict('unknown.claim'))
            out = w.submit_review_request(claimed['packet_id'], req['request_id'], answer,
                                          'host/main', claimed['input_hash'])
            self.assertEqual(out['omitted_claim_ids'], [duplicate['claim_id']])
            self.assertEqual(out['unaccepted_response_row_count'], 3)
            with w.queue.transaction() as state:
                r = w._load(state)
                self.assertNotIn(duplicate['claim_id'], r.receipts)
                self.assertNotIn('unknown.claim', r.receipts)
                self.assertEqual(set(r.receipts), set(req['claim_ids']) - {duplicate['claim_id']})
                sources = state['packets'][claimed['packet_id']]['batch_sources']
                for cid in out['returned_claim_ids']:
                    self.assertIn('attempt_id', sources[cid])
            resumed = NativeReviewWorkflow(w.directory).next()
            self.assertIn(duplicate['claim_id'], resumed['items'])

    def test_dispatcher_renews_long_running_work_before_original_lease_expires(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor, wait as real_wait
        entered, renewed, release = threading.Event(), threading.Event(), threading.Event()
        clock = [100.0]
        waits = [0]
        class WaitingClient:
            identity = 'agy/renewal'
            def complete_structured(self, prompt, schema):
                entered.set()
                if not release.wait(8):
                    raise RuntimeError('test observer did not release client')
                return response(schema['properties']['claim_id']['enum'][0], dict(schema=schema))
        def deterministic_wait(futures, *, timeout, return_when):
            waits[0] += 1
            if waits[0] == 1:
                if not entered.wait(8):
                    raise RuntimeError('client never started')
                clock[0] = 100.75
                return set(), set(futures)
            renewed.set()
            if not release.wait(8):
                raise RuntimeError('observer did not inspect renewed lease')
            return real_wait(futures, timeout=timeout, return_when=return_when)
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            with patch('v4.agent_queue.time.time', side_effect=lambda: clock[0]), \
                 patch('v4.agent_dispatch.wait', side_effect=deterministic_wait):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(dispatch_secondary_reviews, w, WaitingClient(),
                                         max_workers=1, max_items=1, lease_seconds=1)
                    try:
                        self.assertTrue(renewed.wait(8))
                        clock[0] = 101.2
                        with w.queue.transaction() as state:
                            packet = next(p for p in state['packets'].values() if p.get('item_leases'))
                            lease = packet['item_leases']['a']
                            self.assertGreater(lease['expires_at'], clock[0])
                            self.assertEqual(lease['owner'], WaitingClient.identity)
                            packet_id = packet['packet_id']
                        with self.assertRaisesRegex(ValueError, 'already leased'):
                            w.queue.claim_items(packet_id, 'host/competitor', ['a'])
                    finally:
                        release.set()
                    result = future.result(timeout=8)
                    self.assertFalse(result['failures'], result)
                    self.assertEqual(result['accepted_claim_ids'], ['a'])

    def test_accepted_deep_does_not_hide_old_atomic_packet_processing_history(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            atomic = w.claim_reviews('agy/atomic', max_items=8, batch_size=1)
            a = next(req for req in atomic['requests'] if req['claim_ids'] == ['a'])
            w.submit_review_request(atomic['packet_id'], a['request_id'],
                response('a', dict(schema=a['response_schema']), status='SUSPICIOUS'),
                'agy/atomic', atomic['input_hash'])
            w.claim_reviews('host/atomic', max_items=4, batch_size=1)
            deep = w.claim_reviews('host/deep', max_items=1, batch_size=1)
            request = deep['requests'][0]
            w.submit_review_request(deep['packet_id'], request['request_id'], batch_answer(request),
                                    'host/deep', deep['input_hash'])
            w.queue.release_items(atomic['packet_id'], 'agy/atomic', atomic['lease']['lease_id'])
            w.next()
            with w.queue.transaction() as state:
                self.assertTrue(state['packets'][atomic['packet_id']]['retired'])
                r = w._load(state)
                self.assertIn('a', r.receipts)
                self.assertIn('a', r.deep_receipts)
            raw = json.loads(w.queue.state_path.read_text())
            self.assertIn('$native_archive', raw['packets'][atomic['packet_id']]['items'])

    def test_released_owner_cannot_bypass_lease_via_legacy_submit_but_new_host_can(self):
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            claim = w.claim_reviews('agy/old', max_items=1, batch_size=1)
            request = claim['requests'][0]
            answer = batch_answer(request)
            w.queue.release_items(claim['packet_id'], 'agy/old', claim['lease']['lease_id'])
            with self.assertRaisesRegex(ValueError, 'fresh lease'):
                w.submit(claim['packet_id'], {'a': answer}, 'agy/old', claim['input_hash'])
            result = w.submit(claim['packet_id'], {'a': answer}, 'host/new', claim['input_hash'])
            self.assertFalse(result.get('blocked'), result)

    def test_fast_responses_renew_once_at_worker_start(self):
        from .test_agent_dispatch import FakeSecondary
        from .agent_queue import renew_items_in_state
        with tempfile.TemporaryDirectory() as root:
            w = NativeReviews().bootstrap(root)
            with patch('v4.agent_dispatch.renew_items_in_state', wraps=renew_items_in_state) as renew:
                result = dispatch_secondary_reviews(w, FakeSecondary(), max_workers=3, lease_seconds=3600)
            self.assertFalse(result['failures'], result)
            self.assertEqual(len(result['accepted_claim_ids']), 8)
            self.assertEqual(renew.call_count, 8)


class ParallelDependencyIntegration(unittest.TestCase):
    def boot(self, root):
        helper = NativeReviews()
        w = helper.bootstrap(root)
        with w.queue.transaction() as state:
            r = w._load(state)
            r.global_dependency_batch_size = 4
            w._save(state, r)
        first = helper._reach_global(w)
        outcome = w.submit(first['packet_id'], {'global_consistency_reviewer': helper._global_response(w)},
                           'host/global', first['input_hash'])
        self.assertEqual(outcome['status'], 'blocked:global_dependency_coverage', outcome)
        return w

    def answer(self, w, claim):
        with w.queue.transaction() as state:
            r = w._load(state)
            claims = {c['claim_id']: c for c in r.run.claims}
        return dict(findings=[], resolved_external_findings=[], dependency_reviews=[
            dict(claim_id=cid, required_dependencies=claims[cid]['dependencies'],
                 unresolved=False, reason='semantic dependency review fixture')
            for cid in claim['target_claim_ids']])

    def test_out_of_order_chunks_reload_and_preserve_actual_reviewers(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            a = w.claim_dependency_reviews('host/main', max_items=4)
            b = NativeReviewWorkflow(w.directory).claim_dependency_reviews('agy/reviewer', max_items=4)
            from .agent_dispatch import dispatch_status
            reservations = [row for row in dispatch_status(w.directory)['reservations']
                            if row['layer'] == 'dependency']
            self.assertEqual({tuple(row['claim_ids']) for row in reservations},
                             {tuple(sorted(a['target_claim_ids'])), tuple(sorted(b['target_claim_ids']))})
            self.assertFalse(set(a['target_claim_ids']) & set(b['target_claim_ids']))
            self.assertTrue(a['completion_contract']['parallel'])
            self.assertEqual(a['completion_contract']['target_claim_ids'], a['target_claim_ids'])
            self.assertIn('empty arrays', a['completion_contract']['instruction'])
            for claim, owner in [(b, 'agy/reviewer'), (a, 'host/main')]:
                answer = self.answer(w, claim)
                outcome = NativeReviewWorkflow(w.directory).submit(claim['packet_id'],
                    {'global_dependency_completion': answer}, owner, claim['input_hash'])
                self.assertNotEqual(outcome['status'], 'blocked:global_contract', outcome)
            self.assertEqual(outcome['status'], 'verified', outcome)
            for _ in range(2):
                completed = NativeReviewWorkflow(w.directory).next()
                self.assertEqual(completed['status'], 'complete', completed)
                receipt = completed['sidecar']['global_review']
                self.assertEqual(receipt['model'], 'host/global')
                self.assertEqual({receipt['dependency_reviewers'][cid] for cid in b['target_claim_ids']},
                                 {'agy/reviewer'})
                self.assertEqual({receipt['dependency_reviewers'][cid] for cid in a['target_claim_ids']},
                                 {'host/main'})

    def test_secondary_dependency_dispatch_does_not_run_whole_global(self):
        class DependencyClient:
            identity = 'agy/dependency-reviewer'
            def __init__(self, workflow): self.workflow, self.calls = workflow, []
            def complete(self, prompt):
                return self.complete_structured(prompt, None)
            def complete_structured(self, prompt, schema):
                from .review_budget import _prompt_payload
                payload = _prompt_payload(prompt)
                ids = payload['target_claim_ids']
                self.calls.append(ids)
                return ParallelDependencyIntegration().answer(self.workflow, dict(target_claim_ids=ids))
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            client = DependencyClient(w)
            result = dispatch_secondary_reviews(w, client, max_workers=2, max_items=20,
                                                batch_size=4, continuous=True)
            self.assertFalse(result['failures'], result)
            self.assertEqual(len(result['accepted_claim_ids']), 8)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(NativeReviewWorkflow(w.directory).next()['status'], 'complete')

    def test_released_dependency_packet_cannot_deliver_after_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            old = w.claim_dependency_reviews('agy/old', max_items=4)
            answer = self.answer(w, old)
            w.queue.release_items(old['packet_id'], 'agy/old', old['lease']['lease_id'])
            new = w.claim_dependency_reviews('host/main', max_items=4)
            self.assertEqual(old['target_claim_ids'], new['target_claim_ids'])
            with self.assertRaisesRegex(ValueError, 'active lease'):
                w.submit(old['packet_id'], {'global_dependency_completion': answer},
                         'agy/old', old['input_hash'])
            accepted = w.submit(new['packet_id'], {'global_dependency_completion': answer},
                                'host/main', new['input_hash'])
            self.assertNotEqual(accepted['status'], 'blocked:global_contract', accepted)

    def test_parallel_revision_closure_never_reissues_final_chunk_or_blocks_patch_reload(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            first = w.claim_dependency_reviews('host/main', max_items=4)
            second = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            response_second = self.answer(w, second)
            target = response_second['dependency_reviews'][-1]
            target['required_dependencies'] = list(dict.fromkeys(target['required_dependencies'] + ['independent']))
            w.submit(second['packet_id'], {'global_dependency_completion': response_second},
                     'agy/reviewer', second['input_hash'])
            outcome = w.submit(first['packet_id'], {'global_dependency_completion': self.answer(w, first)},
                               'host/main', first['input_hash'])
            self.assertEqual(outcome['status'], 'revision', outcome)
            packet = NativeReviewWorkflow(w.directory).next()
            self.assertEqual(packet['kind'], 'patch', packet)
            for _ in range(2):
                resumed = NativeReviewWorkflow(w.directory).next()
                self.assertEqual(resumed['packet_id'], packet['packet_id'])
                self.assertEqual(resumed['kind'], 'patch')

    def test_dependency_work_ready_is_not_a_failed_cli_exit(self):
        from .test_agent_cli import run
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            w.claim_dependency_reviews('host/main', max_items=4)
            rc, ready = run(['next', '--work-dir', str(w.directory)])
            self.assertEqual(rc, 0, ready)
            self.assertEqual(ready['status'], 'global_dependency_ready')
            self.assertFalse(ready['blocked'])
            self.assertFalse(ready['delivery_ready'])

    def test_parallel_claim_waits_for_live_legacy_completion_owner(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            legacy = w.next()
            role = 'global_dependency_completion'
            w.queue.claim_items(legacy['packet_id'], 'host/global', [role])
            with w.queue.transaction() as state:
                before = len(state['packets'])
            waiting = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            self.assertEqual(waiting['status'], 'global_dependency_waiting')
            with w.queue.transaction() as state:
                self.assertEqual(before, len(state['packets']))
            result = w.submit(legacy['packet_id'], {role: NativeReviews()._global_response(w)},
                              'host/global', legacy['input_hash'])
            self.assertNotEqual(result['status'], 'blocked:global_contract')
            claimed = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            self.assertEqual(claimed['status'], 'dependency_reviews_claimed')

    def test_released_dependency_attempts_are_retired_and_cold_archived(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            abandoned = []
            for _ in range(3):
                claim = w.claim_dependency_reviews('agy/reviewer', max_items=4)
                abandoned.append(claim['packet_id'])
                w.queue.release_items(claim['packet_id'], 'agy/reviewer', claim['lease']['lease_id'])
            w.claim_dependency_reviews('host/main', max_items=4)
            with w.queue.transaction() as state:
                self.assertTrue(all(state['packets'][pid]['retired'] for pid in abandoned))
            raw = json.loads(w.queue.state_path.read_text())
            self.assertTrue(all('$native_archive' in raw['packets'][pid]['items'] for pid in abandoned))

    def test_preupgrade_received_dependency_candidate_is_collected_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            claim = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            answer = self.answer(w, claim)
            # Simulate the old transport's crash between receipt storage and
            # canonical collection; the new public submit uses one transaction.
            w.queue.submit(claim['packet_id'], {'global_dependency_completion': answer},
                           'agy/reviewer', claim['input_hash'])
            result = NativeReviewWorkflow(w.directory).next()
            self.assertNotEqual(result['status'], 'blocked:global_contract', result)
            with w.queue.transaction() as state:
                self.assertIn(claim['packet_id'], state['global_outcomes'])
                r = w._load(state)
                self.assertFalse(set(claim['target_claim_ids']) & set(
                    r._global_coverage_status()['missing_dependency_claim_ids']))

    def test_exact_global_redelivery_is_idempotent_after_reload(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            claim = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            answers = {'global_dependency_completion': self.answer(w, claim)}
            first = w.submit(claim['packet_id'], answers, 'agy/reviewer', claim['input_hash'])
            again = NativeReviewWorkflow(w.directory).submit(claim['packet_id'], answers,
                                                             'agy/reviewer', claim['input_hash'])
            self.assertTrue(again.pop('idempotent'))
            self.assertEqual(first, again)
            changed = json.loads(json.dumps(answers))
            changed['global_dependency_completion']['dependency_reviews'][0]['reason'] = 'different answer'
            with self.assertRaisesRegex(ValueError, 'conflicting duplicate'):
                w.submit(claim['packet_id'], changed, 'agy/reviewer', claim['input_hash'])

    def test_parallel_claim_waits_for_received_legacy_candidate_until_next_ingests(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            legacy = w.next()
            role = 'global_dependency_completion'
            answer = NativeReviews()._global_response(w)
            w.queue.submit(legacy['packet_id'], {role: answer}, 'host/global', legacy['input_hash'])
            waiting = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            self.assertEqual(waiting['status'], 'global_dependency_waiting')
            result = NativeReviewWorkflow(w.directory).next()
            self.assertNotEqual(result['status'], 'blocked:global_contract', result)
            claimed = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            self.assertEqual(claimed['status'], 'dependency_reviews_claimed')
            self.assertFalse({row['claim_id'] for row in answer['dependency_reviews']}
                             & set(claimed['target_claim_ids']))

    def test_blocked_global_redelivery_returns_same_block_without_retrying_collector(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            claim = w.claim_dependency_reviews('agy/reviewer', max_items=4)
            invalid = {'global_dependency_completion': {'not_dependency_reviews': []}}
            first = w.submit(claim['packet_id'], invalid, 'agy/reviewer', claim['input_hash'])
            self.assertEqual(first['status'], 'blocked:global_contract')
            repeated = NativeReviewWorkflow(w.directory).submit(claim['packet_id'], invalid,
                                                                'agy/reviewer', claim['input_hash'])
            self.assertTrue(repeated.pop('idempotent'))
            self.assertEqual(repeated, first)

    def test_direct_dependency_claim_drains_received_candidate_without_duplicate_scope(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            old = w.claim_dependency_reviews('agy/first', max_items=4)
            answer = self.answer(w, old)
            w.queue.submit(old['packet_id'], {'global_dependency_completion': answer},
                           'agy/first', old['input_hash'])
            new = NativeReviewWorkflow(w.directory).claim_dependency_reviews('host/main', max_items=4)
            self.assertEqual(new['status'], 'dependency_reviews_claimed')
            self.assertFalse(set(old['target_claim_ids']) & set(new['target_claim_ids']))
            with w.queue.transaction() as state:
                self.assertIn(old['packet_id'], state['global_outcomes'])
                self.assertTrue(state['packets'][old['packet_id']]['retired'])

    def test_failed_chunk_cannot_be_reclaimed_until_explicit_retry_and_siblings_survive(self):
        with tempfile.TemporaryDirectory() as root:
            w = self.boot(root)
            bad = w.claim_dependency_reviews('agy/first', max_items=4)
            sibling = w.claim_dependency_reviews('host/sibling', max_items=4)
            failed = w.submit(bad['packet_id'], {'global_dependency_completion': {'invalid': []}},
                              'agy/first', bad['input_hash'])
            self.assertEqual(failed['status'], 'blocked:global_contract')
            valid = w.submit(sibling['packet_id'],
                {'global_dependency_completion': self.answer(w, sibling)}, 'host/sibling', sibling['input_hash'])
            self.assertNotEqual(valid['status'], 'blocked:global_contract', valid)
            with w.queue.transaction() as state:
                count = len(state['packets'])
                self.assertIsNone(w._load(state).global_receipt)
            blocked = w.claim_dependency_reviews('host/retry', max_items=4)
            self.assertEqual(blocked['status'], 'blocked:global_contract')
            with w.queue.transaction() as state:
                self.assertEqual(count, len(state['packets']))
            w.retry_global('operator corrects malformed dependency response')
            retry = w.claim_dependency_reviews('host/retry', max_items=4)
            self.assertEqual(retry['target_claim_ids'], bad['target_claim_ids'])
            done = w.submit(retry['packet_id'],
                {'global_dependency_completion': self.answer(w, retry)}, 'host/retry', retry['input_hash'])
            self.assertEqual(done['status'], 'verified', done)
            self.assertEqual(NativeReviewWorkflow(w.directory).next()['status'], 'complete')
