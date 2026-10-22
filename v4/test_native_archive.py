"""Lossless native hot-state storage; never tests model verdict correctness."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .agent_queue import NativeWorkQueue


class NativeArchiveTests(unittest.TestCase):
    def seed(self, directory):
        queue = NativeWorkQueue(directory)
        queue.create({'original': ['unchanged', 3]}, 'hash-a')
        packet = queue.issue('review', {
            'z-role': {'prompt': '共用證據\n' * 10000, 'schema': {'z': 1, 'a': [2, 1]}},
            'a-role': {'prompt': 'exact other prompt', 'schema': ['z', 'a']},
        }, 'hash-a')
        queue.submit(packet['packet_id'], {'a-role': {'z': ['PASS', 'FAIL'], 'a': 1}},
                     'executor/original', 'hash-a')
        with queue.transaction() as state:
            state['packets'][packet['packet_id']]['retired'] = True
            state['patch_outcomes'] = {'p-old': {'status': 'retry', 'prompts': ['repair\n' * 30000]}}
            expected = deepcopy(state)
        return queue, packet['packet_id'], expected

    def disk(self, queue):
        return json.loads(queue.state_path.read_text())

    def archives(self, queue):
        return sorted((queue.directory / 'native-archive').glob('*.json'))

    def test_retired_and_outcomes_compact_but_transactions_are_exact_and_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet_id, expected = self.seed(directory)
            self.assertLess(queue.state_path.stat().st_size, 3000)
            self.assertTrue(self.archives(queue))
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.archives(queue)}
            with NativeWorkQueue(directory).transaction() as actual:
                self.assertEqual(actual, expected)
                self.assertEqual(json.dumps(actual, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
                self.assertEqual(list(actual['packets'][packet_id]['items']), ['z-role', 'a-role'])
                self.assertEqual(list(actual['packets'][packet_id]['received']), ['a-role'])
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.archives(queue)})
            for path in self.archives(queue):
                self.assertEqual(path.stem, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_old_inline_state_migrates_only_on_successful_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, _, expected = self.seed(directory)
            queue.state_path.write_text(json.dumps(expected, ensure_ascii=False))
            inline = queue.state_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'abort'):
                with queue.transaction() as actual:
                    self.assertEqual(actual, expected)
                    raise RuntimeError('abort')
            self.assertEqual(queue.state_path.read_bytes(), inline)
            with queue.transaction() as actual:
                self.assertEqual(actual, expected)
            self.assertLess(queue.state_path.stat().st_size, 3000)

    def test_legacy_inline_state_upgrades_and_previous_reader_rejects_archive_format(self):
        from .agent_queue import STATE_FORMAT, LEGACY_STATE_FORMAT
        self.assertNotEqual(STATE_FORMAT, LEGACY_STATE_FORMAT)
        with tempfile.TemporaryDirectory() as directory:
            queue, _, expected = self.seed(directory)
            legacy = deepcopy(expected)
            legacy['format'] = LEGACY_STATE_FORMAT
            queue.state_path.write_text(json.dumps(legacy))
            with queue.transaction() as hydrated:
                self.assertEqual(hydrated['format'], STATE_FORMAT)
            disk = self.disk(queue)
            self.assertEqual(disk['format'], STATE_FORMAT)
            self.assertIn('_native_archive', disk)
            self.assertNotEqual(disk['format'], LEGACY_STATE_FORMAT)

    def test_historical_v1_reader_rejects_v2_archived_state(self):
        # Execute a frozen copy of the real v1 reader from commit 90363b7,
        # so the regression also runs in shallow CI checkouts.
        from types import ModuleType
        snapshot = (Path(__file__).parent / 'test_fixtures' / 'native_queue_v1.py.txt').read_bytes()
        self.assertEqual(hashlib.sha256(snapshot).hexdigest(),
            '3373cdf262182ab9d3d49a74f93a628c86ca800d02d2c205891b62a28bd03578')
        old = ModuleType('v4.agent_queue_v1_test_reader')
        old.__package__ = 'v4'
        exec(compile(snapshot, 'v4/agent_queue_v1_snapshot.py', 'exec'), old.__dict__)
        self.assertEqual(old.STATE_FORMAT, 'v4-native-work/1')
        with tempfile.TemporaryDirectory() as directory:
            queue, _, _ = self.seed(directory)
            self.assertEqual(self.disk(queue)['format'], 'v4-native-work/2')
            self.assertIn('_native_archive', self.disk(queue))
            with self.assertRaisesRegex(ValueError, 'unsupported native work queue state'):
                with old.NativeWorkQueue(directory).transaction():
                    self.fail('previous version accepted a compacted /2 state')

    def test_mutating_or_unretiring_hydrated_packet_keeps_exact_content(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet_id, expected = self.seed(directory)
            with queue.transaction() as state:
                state['packets'][packet_id]['retired'] = False
                state['packets'][packet_id]['items']['z-role']['prompt'] += 'new suffix'
                expected = deepcopy(state)
            self.assertEqual(self.disk(queue)['packets'][packet_id]['items'], expected['packets'][packet_id]['items'])
            with queue.transaction() as state:
                self.assertEqual(state, expected)

    def test_missing_tampered_symlink_and_unsafe_digest_ref_fail_without_state_write(self):
        for failure in ('missing', 'tampered', 'symlink', 'unsafe', 'manifest_removed', 'directory_symlink'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                queue, _, _ = self.seed(directory)
                paths = self.archives(queue)
                self.assertTrue(paths, 'expected immutable archive payloads')
                if failure == 'missing':
                    paths[0].unlink()
                elif failure == 'tampered':
                    paths[0].chmod(0o644)
                    paths[0].write_text('{}')
                elif failure == 'symlink':
                    payload = paths[0].read_bytes()
                    paths[0].unlink()
                    target = Path(directory) / 'outside.json'
                    target.write_bytes(payload)
                    paths[0].symlink_to(target)
                elif failure in ('unsafe', 'manifest_removed'):
                    disk = self.disk(queue)
                    if failure == 'unsafe':
                        disk['_native_archive']['refs'][0]['sha256'] = '../escape'
                    else:
                        del disk['_native_archive']
                    queue.state_path.write_text(json.dumps(disk))
                else:
                    archive_dir = queue.directory / 'native-archive'
                    renamed = queue.directory / 'relocated'
                    archive_dir.rename(renamed)
                    archive_dir.symlink_to(renamed, target_is_directory=True)
                before = queue.state_path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'native archive'):
                    with queue.transaction():
                        self.fail('invalid evidence must fail before exposing state')
                self.assertEqual(queue.state_path.read_bytes(), before)

    def test_archive_is_durable_before_state_write_and_retry_reuses_orphans(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = NativeWorkQueue(directory)
            queue.create({}, 'hash-a')
            packet = queue.issue('review', {'one': {'prompt': 'evidence' * 10000}}, 'hash-a')
            before = queue.state_path.read_bytes()
            def fail_write(path, state):
                self.assertTrue(self.archives(queue))
                raise OSError('simulated state replace failure')
            with patch('v4.agent_queue.write_json', side_effect=fail_write):
                with self.assertRaisesRegex(OSError, 'replace failure'):
                    with queue.transaction() as state:
                        state['packets'][packet['packet_id']]['retired'] = True
            self.assertEqual(queue.state_path.read_bytes(), before)
            orphaned = {p.name: p.stat().st_mtime_ns for p in self.archives(queue)}
            with queue.transaction() as state:
                state['packets'][packet['packet_id']]['retired'] = True
            self.assertEqual(orphaned, {p.name: p.stat().st_mtime_ns for p in self.archives(queue)})

    def test_distinct_prompts_share_exact_frame_prefix_and_verify_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = NativeWorkQueue(directory)
            queue.create({}, 'hash-a')
            prefix = 'exact shared context 漢字\n' * 1000 + '\n\n```json\n'
            for suffix in ('{"claim":"one"}\n```', '{"claim":"two"}\n```'):
                packet = queue.issue('review', {'role': {'prompt': prefix + suffix}}, 'hash-a')
                with queue.transaction() as state:
                    state['packets'][packet['packet_id']]['retired'] = True
            files = self.archives(queue)
            envelopes = [json.loads(path.read_bytes()) for path in files]
            self.assertEqual(sum(row.get('value') == prefix for row in envelopes), 1)
            pooled = [row for row in envelopes if row.get('prompt_prefixes')]
            self.assertEqual(len(pooled), 2)
            self.assertEqual(len({row['prompt_prefixes'][0]['sha256'] for row in pooled}), 1)
            self.assertLess(sum(path.stat().st_size for path in files), len(prefix.encode()) * 1.2)
            with queue.transaction() as state:
                self.assertEqual([p['items']['role']['prompt'] for p in state['packets'].values()],
                                 [prefix + '{"claim":"one"}\n```', prefix + '{"claim":"two"}\n```'])

    def test_reconstructed_logical_hash_is_checked_even_when_file_hash_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, _, _ = self.seed(directory)
            disk = self.disk(queue)
            reference = disk['_native_archive']['refs'][0]
            old_digest = reference['sha256']
            archive_path = queue.directory / 'native-archive' / (old_digest + '.json')
            envelope = json.loads(archive_path.read_bytes())
            envelope['logical_sha256'] = '0' * 64
            payload = json.dumps(envelope).encode()
            new_digest = hashlib.sha256(payload).hexdigest()
            archive_path.with_name(new_digest + '.json').write_bytes(payload)
            reference['sha256'] = new_digest
            slot = disk
            for part in reference['path'][:-1]:
                slot = slot[part]
            slot[reference['path'][-1]] = {'$native_archive': new_digest}
            queue.state_path.write_text(json.dumps(disk))
            with self.assertRaisesRegex(ValueError, 'logical content hash'):
                with queue.transaction():
                    self.fail('logical checksum mismatch must not expose evidence')

    def test_legacy_item_id_matching_storage_marker_is_lossless(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = NativeWorkQueue(directory)
            queue.create({}, 'hash-a')
            packet = queue.issue('review', {'$native_archive': {'prompt': 'valid exact item ID'}}, 'hash-a')
            with queue.transaction() as state:
                state['packets'][packet['packet_id']]['retired'] = True
                expected = deepcopy(state)
            with queue.transaction() as actual:
                self.assertEqual(actual, expected)

    def test_identical_retired_payloads_deduplicate_without_losing_packet_order(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet_id, _ = self.seed(directory)
            before = len(self.archives(queue))
            with queue.transaction() as state:
                duplicate = deepcopy(state['packets'][packet_id])
                duplicate['packet_id'] = 'second-packet'
                state['packets']['second-packet'] = duplicate
                expected = deepcopy(state)
            self.assertEqual(len(self.archives(queue)), before)
            with queue.transaction() as actual:
                self.assertEqual(actual, expected)
                self.assertEqual(list(actual['packets']), [packet_id, 'second-packet'])


if __name__ == '__main__':
    unittest.main()
