"""Supervision contract with fake child processes; no model or network calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from .dispatch_supervisor import supervise_secondary


class DispatchSupervisor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'native'
        self.root.mkdir()
        (self.root / 'agent-state.json').write_text('{}', encoding='utf-8')

    def fake_child(self, status, exit_code=0, *, wait_once=False):
        class Child:
            pid = 72312
            def __init__(self):
                self.calls = 0
            def wait(self, timeout):
                self.calls += 1
                if wait_once and self.calls == 1:
                    time.sleep(0.03)
                    raise subprocess.TimeoutExpired('fixture', timeout)
                return exit_code
        def launch(command, stdout, stderr, text):
            self.assertEqual(command[1:4], ['-B', '-m', 'v4.agent_cli'])
            self.assertIn('dispatch-secondary', command)
            stdout.write(json.dumps(dict(status=status)))
            stdout.flush()
            return Child()
        return launch

    def test_success_keeps_durable_process_and_result(self):
        with patch('v4.dispatch_supervisor.subprocess.Popen', side_effect=self.fake_child('secondary_complete')):
            result = supervise_secondary(self.root, 'fake', allow_ephemeral=True)
        self.assertEqual(result['status'], 'secondary_complete')
        row = result['supervision']
        self.assertEqual((row['status'], row['child_pid'], row['exit_code']), ('exited', 72312, 0))
        self.assertTrue(Path(row['stdout_path']).is_file())
        self.assertEqual(json.loads(Path(row['stdout_path']).read_text())['status'], 'secondary_complete')
        self.assertEqual(json.loads((Path(row['stdout_path']).parent / 'status.json').read_text()), row)

    def test_start_failure_and_nonzero_exit_fall_back_to_main(self):
        with patch('v4.dispatch_supervisor.subprocess.Popen', side_effect=OSError('unavailable')):
            result = supervise_secondary(self.root, 'fake', allow_ephemeral=True)
        self.assertEqual(result['status'], 'fallback_to_main')
        self.assertEqual(result['supervision']['status'], 'start_failed')
        with patch('v4.dispatch_supervisor.subprocess.Popen', side_effect=self.fake_child('secondary_complete', 9)):
            result = supervise_secondary(self.root, 'fake', allow_ephemeral=True)
        self.assertEqual(result['status'], 'fallback_to_main')
        self.assertEqual(result['supervision']['exit_code'], 9)

    def test_single_packet_option_reaches_child(self):
        launch = self.fake_child('secondary_complete')
        with patch('v4.dispatch_supervisor.subprocess.Popen', side_effect=launch) as mocked:
            supervise_secondary(self.root, 'fake', single_packet=True, allow_ephemeral=True)
        self.assertIn('--single-packet', mocked.call_args.args[0])

    def test_missing_heartbeat_alerts_but_never_cancels_child(self):
        child = self.fake_child('fallback_to_main', wait_once=True)
        with patch('v4.dispatch_supervisor.subprocess.Popen', side_effect=child), \
             patch('v4.dispatch_supervisor.dispatch_status', return_value={'dispatchers': []}):
            result = supervise_secondary(self.root, 'fake', poll_seconds=0.01,
                                         attention_after_seconds=0.01, allow_ephemeral=True)
        self.assertEqual(result['status'], 'fallback_to_main')
        self.assertEqual(result['supervision']['exit_code'], 0)
        self.assertIn('last_dispatch_heartbeat', result['supervision'])
        self.assertEqual(len(result['supervision']['attention_events']), 1)

    def test_temporary_work_dir_rejected_before_child_start(self):
        with patch('v4.dispatch_supervisor.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'persistent'):
                supervise_secondary(self.root, 'fake')
        launch.assert_not_called()

    def test_real_child_reports_unavailable_agy_without_starting_a_model(self):
        from .test_agent_reviews import NativeReviews
        real_root = self.root / 'real'
        real_root.mkdir()
        workflow = NativeReviews().bootstrap(real_root)
        missing = str(Path(self.temp.name) / 'no-agy-executable')
        with patch.dict(os.environ, {'AGY_EXECUTABLE': missing}):
            result = supervise_secondary(workflow.directory, 'fake', poll_seconds=0.1,
                                         attention_after_seconds=10, allow_ephemeral=True)
        self.assertEqual(result['status'], 'fallback_to_main')
        self.assertEqual(result['supervision']['status'], 'exited')
        self.assertEqual(result['supervision']['exit_code'], 0)
        self.assertEqual(result['packet']['kind'], 'atomic')

    def test_existing_dispatch_cli_uses_real_supervisor_by_default(self):
        import contextlib
        import io
        from .agent_cli import main
        from .test_agent_reviews import NativeReviews
        real_root = self.root / 'cli-real'
        real_root.mkdir()
        workflow = NativeReviews().bootstrap(real_root)
        missing = str(Path(self.temp.name) / 'no-agy-executable')
        output = io.StringIO()
        with patch.dict(os.environ, {'AGY_EXECUTABLE': missing}), contextlib.redirect_stdout(output):
            code = main(['dispatch-secondary', '--work-dir', str(workflow.directory),
                         '--agy-model', 'fake', '--allow-ephemeral-work-dir',
                         '--progress-interval-seconds', '0.1'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'fallback_to_main')
        self.assertEqual(result['supervision']['status'], 'exited')
        self.assertEqual(result['supervision']['exit_code'], 0)
