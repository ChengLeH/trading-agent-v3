import json
import tempfile
import unittest
from pathlib import Path

from v4.task_delivery import create_delivery, materialize_delivery, query_delivery, delivery_index, review_brief, review_directory, review_query, review_claim, task_brief


class DeliveryTest(unittest.TestCase):
    def test_lossless_shared_role_and_query_after_restart(self):
        context = {'role': 'analyst', 'scope': {'as_of': '2026-10-28'},
                   'facts': {'f1': {'value': 12, 'scope': None}},
                   'calcs': {}, 'evidence': {'e1': {'text': '原始資料'}}, 'events': {},
                   'claims': [{'claim_id': 'c1', 'text': '原文', 'refs': ['e1']}]}
        def prompt(role):
            data = dict(context, role=role)
            return 'instructions\n\n<context-data>\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n</context-data>\nretry 原文'
        packet = {'kind': 'roles', 'input_hash': 'unchanged', 'items': {
            'a': {'prompt': prompt('a')}, 'b': {'prompt': prompt('b')},
            'review': {'prompt': 'opaque review', 'response_schema': {'type': 'object'}}}}
        with tempfile.TemporaryDirectory() as tmp:
            pointer = create_delivery(tmp, packet)
            pointer = json.loads(json.dumps(pointer))
            self.assertEqual(materialize_delivery(pointer), packet)
            self.assertEqual(delivery_index(pointer, 'a')['facts'], ['f1'])
            self.assertEqual(query_delivery(pointer, 'a', 'context', 'scope'), context['scope'])
            self.assertEqual(query_delivery(pointer, 'a', 'facts', 'f1'), context['facts']['f1'])
            self.assertEqual(query_delivery(pointer, 'a', 'claims', 'c1'), context['claims'][0])
            with self.assertRaises(KeyError):
                query_delivery(pointer, 'a', 'facts', 'missing')
            again = create_delivery(tmp, packet)
            self.assertEqual(pointer, again)

    def test_corrupt_and_missing_objects_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pointer = create_delivery(tmp, {'items': {'x': {'prompt': 'original'}}})
            manifest_path = Path(pointer['manifest_file'])
            manifest = json.loads(manifest_path.read_bytes())
            path = manifest_path.parent / manifest['items']['x']['prompt']['object']
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'corrupt'):
                materialize_delivery(pointer)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                materialize_delivery(pointer)
            create_delivery(tmp, {'items': {'x': {'prompt': 'original'}}})
            self.assertEqual(materialize_delivery(pointer)['items']['x']['prompt'], 'original')
            manifest_path.write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError, 'corrupt'):
                materialize_delivery(pointer)

    def test_real_atomic_batch_and_global_prompts(self):
        from v4.test_integration import seeded, review_all
        from v4.atomic_phase6 import build_atomic_prompt
        from v4.review_batches import pack_review_requests
        runner = seeded()
        tasks = list(runner._tasks().values())
        atomic = [build_atomic_prompt(t, verification_epoch='epoch') for t in tasks[:2]]
        batch = pack_review_requests(tasks[:2], batch_size=2, max_bytes=1000000,
                                     verification_epoch='epoch')[0]
        review_all(runner)
        _, global_prompt = runner.prepare_phase6_global_sweep()
        packet = {'items': {'atomic0': {'prompt': atomic[0]},
                            'atomic1': {'prompt': atomic[1]},
                            'batch': {'prompt': batch['prompt'], 'response_schema': batch['response_schema']},
                            'global': {'prompt': global_prompt}},
                  'requests': [batch]}
        with tempfile.TemporaryDirectory() as tmp:
            pointer = create_delivery(tmp, packet)
            self.assertEqual(materialize_delivery(pointer), packet)
            manifest = json.loads(Path(pointer['manifest_file']).read_bytes())
            p0 = manifest['items']['atomic0']['prompt']
            p1 = manifest['items']['atomic1']['prompt']
            self.assertEqual(p0['type'], 'review_context')
            self.assertEqual(p0['prefix'], p1['prefix'])
            self.assertGreater(len(review_brief(pointer, 'atomic0')['instructions'].encode()), 1000)
            self.assertEqual(review_brief(pointer, 'batch')['item']['response_schema'], batch['response_schema'])
            self.assertEqual(review_brief(pointer, batch['request_id'])['item']['response_schema'], batch['response_schema'])
            self.assertEqual(review_query(pointer, batch['request_id'], ['tasks', 0]),
                             review_query(pointer, 'batch', ['tasks', 0]))
            payload = json.loads(batch['prompt'].split('```json\n', 1)[1].split('\n```', 1)[0])
            self.assertEqual(review_query(pointer, 'batch', ['tasks', 0]), payload['tasks'][0])
            cid = payload['tasks'][0]['claim']['claim_id']
            self.assertEqual(review_claim(pointer, 'batch', cid), payload['tasks'][0])
            self.assertEqual(review_directory(pointer, 'batch', ['evidence_catalog']), list(payload['evidence_catalog']))
            for key, value in payload['evidence_catalog'].items():
                self.assertEqual(review_query(pointer, 'batch', ['evidence_catalog', key]), value)
            for key, value in payload['dependency_catalog'].items():
                self.assertEqual(review_query(pointer, 'batch', ['dependency_catalog', key]), value)
            global_data = json.loads(global_prompt.split('<context-data>\n', 1)[1].split('\n</context-data>', 1)[0])
            self.assertEqual(review_directory(pointer, 'global', ['state', 'facts']), list(global_data['state']['facts']))
            for key, value in global_data['state']['facts'].items():
                self.assertEqual(review_query(pointer, 'global', ['state', 'facts', key]), value)
            self.assertEqual(review_query(pointer, 'global', ['state', 'claims', 0]), global_data['state']['claims'][0])
            first_claim = global_data['state']['claims'][0]
            self.assertEqual(review_claim(pointer, 'global', first_claim['claim_id'], global_state=True), first_claim)
            prefix = Path(pointer['manifest_file']).parent / p0['prefix']
            prefix.write_bytes(b'damaged')
            with self.assertRaisesRegex(ValueError, 'corrupt'):
                materialize_delivery(pointer)
            prefix.unlink()
            with self.assertRaises(FileNotFoundError):
                materialize_delivery(pointer)

    def test_nonempty_batch_catalogs_and_manifest_only_brief(self):
        from v4.atomic_phase6 import AtomicTask
        from v4.review_batches import pack_review_requests
        claim = {'claim_id': 'child', 'text': 'Child', 'refs': ['ev1'],
                 'fact_refs': [], 'dependencies': ['parent'], 'bindings': []}
        parent = {'claim_id': 'parent', 'text': 'Parent', 'refs': [],
                  'fact_refs': [], 'dependencies': [], 'bindings': []}
        task = AtomicTask('child', claim, {'ev1': {'kind': 'evidence', 'text': '完整來源'}}, [parent], [])
        request = pack_review_requests([task], batch_size=1, max_bytes=1000000,
                                       verification_epoch='epoch')[0]
        payload = json.loads(request['prompt'].split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertTrue(payload['evidence_catalog'])
        self.assertTrue(payload['dependency_catalog'])
        with tempfile.TemporaryDirectory() as tmp:
            pointer = create_delivery(tmp, {'items': {'analyst': {'prompt': 'p'}}, 'requests': [request]})
            rid = request['request_id']
            self.assertEqual(review_query(pointer, rid, ['evidence_catalog', 'ev1']), payload['evidence_catalog']['ev1'])
            self.assertEqual(review_query(pointer, rid, ['dependency_catalog', 'parent']), payload['dependency_catalog']['parent'])
            manifest = json.loads(Path(pointer['manifest_file']).read_bytes())
            for h in [manifest['packet'], manifest['items']['analyst']['item'],
                      manifest['requests'][0][1]['item']]:
                (Path(pointer['manifest_file']).parent / h).unlink(missing_ok=True)
            self.assertEqual(task_brief(pointer), {'items': [{'item_id': 'analyst'}],
                             'requests': [{'request_id': rid, 'claim_ids': ['child']}]})

    def test_packet_without_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(materialize_delivery(create_delivery(tmp, {'status': 'done'})), {'status': 'done'})


if __name__ == '__main__':
    unittest.main()
