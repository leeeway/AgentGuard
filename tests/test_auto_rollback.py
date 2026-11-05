import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard import core
from agentguard.cli import main


class AutomaticRollback(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('source').write_text('human work')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, code, enabled=True):
        flags = ['--rollback-on-failure'] if enabled else []
        return self.runner.invoke(main, ['run'] + flags + ['--', sys.executable, '-c', code])

    def test_failure_restores_and_retains_exit_and_diff(self):
        result = self.execute("from pathlib import Path; Path('source').write_text('agent'); Path('new').touch(); raise SystemExit(7)")
        self.assertEqual(result.exit_code, 7, result.output)
        self.assertEqual(Path('source').read_text(), 'human work')
        self.assertFalse(Path('new').exists())
        row = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertEqual(row['state'], 'undone')
        self.assertEqual(row['exit_code'], 7)
        self.assertEqual(row['automatic_rollback'], 'restored')
        self.assertIn('+agent', self.runner.invoke(main, ['diff', '--contents']).output)
        self.assertFalse(Path('.agentguard/session.lock').exists())

    def test_success_keeps_changes(self):
        result = self.execute("from pathlib import Path; Path('source').write_text('success')")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(Path('source').read_text(), 'success')
        self.assertEqual(core.records(Path.cwd())[-1]['automatic_rollback'], 'not-needed')

    def test_default_failure_keeps_changes(self):
        result = self.execute("from pathlib import Path; Path('source').write_text('failed'); raise SystemExit(3)", enabled=False)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(Path('source').read_text(), 'failed')
        self.assertEqual(core.records(Path.cwd())[-1]['automatic_rollback'], 'disabled')

    def test_conflict_blocks_recovery_and_is_recorded(self):
        original_restore = core.restore
        def concurrent_edit(root, record):
            Path('source').write_text('later human edit')
            return original_restore(root, record)
        with patch('agentguard.cli.core.restore', side_effect=concurrent_edit):
            result = self.execute("from pathlib import Path; Path('source').write_text('agent'); raise SystemExit(7)")
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Command exited 7; automatic rollback failed', result.output)
        self.assertEqual(Path('source').read_text(), 'later human edit')
        self.assertEqual(core.records(Path.cwd())[-1]['automatic_rollback'], 'failed')

    def test_signal_exit_restores_and_returns_shell_status(self):
        result = self.execute("from pathlib import Path; import os, signal; Path('source').unlink(); os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(result.exit_code, 143)
        self.assertEqual(Path('source').read_text(), 'human work')
        self.assertEqual(core.records(Path.cwd())[-1]['exit_code'], -15)

    def test_launch_failure_does_not_attempt_recovery(self):
        with patch('agentguard.cli.core.restore') as restore:
            result = self.runner.invoke(main, ['run', '--rollback-on-failure', '--', '/nonexistent/agentguard-test'])
        self.assertNotEqual(result.exit_code, 0)
        restore.assert_not_called()
        self.assertEqual(core.records(Path.cwd())[-1]['automatic_rollback'], 'not-started')
        self.assertEqual(Path('source').read_text(), 'human work')

    def test_earlier_session_is_not_undone(self):
        self.execute("from pathlib import Path; Path('source').write_text('earlier')")
        self.execute("from pathlib import Path; Path('source').write_text('failed'); raise SystemExit(2)")
        self.assertEqual(Path('source').read_text(), 'earlier')
        self.assertFalse(core.records(Path.cwd())[0].get('undone', False))
