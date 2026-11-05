import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch
from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core


def python(code):
    return shlex.join([sys.executable, '-c', code])


class Verification(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('source').write_text('before')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, validators, source="from pathlib import Path; Path('source').write_text('agent')", rollback=False):
        flags = ['--rollback-on-failure'] if rollback else []
        for command in validators:
            flags.extend(['--verify', command])
        return self.runner.invoke(main, ['run'] + flags + ['--', sys.executable, '-c', source])

    def test_ordered_checks_use_agent_changes_and_record_success(self):
        result = self.execute([python("from pathlib import Path; assert Path('source').read_text() == 'agent'; Path('checked').touch()"), python("from pathlib import Path; assert Path('checked').exists()")])
        self.assertEqual(result.exit_code, 0, result.output)
        row = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertEqual(row['verification'], {'requested': 2, 'exit_codes': [0, 0], 'state': 'passed'})
        self.assertEqual(row['command_exit_code'], 0)
        self.assertEqual(Path('source').read_text(), 'agent')

    def test_failed_check_rolls_back_agent_and_validator_changes(self):
        result = self.execute([python("from pathlib import Path; Path('artifact').touch(); raise SystemExit(9)"), python("from pathlib import Path; Path('skipped').touch()")], rollback=True)
        self.assertEqual(result.exit_code, 9, result.output)
        self.assertEqual(Path('source').read_text(), 'before')
        self.assertFalse(Path('artifact').exists())
        self.assertFalse(Path('skipped').exists())
        record = core.records(Path.cwd())[-1]
        self.assertEqual(record['command_exit_code'], 0)
        self.assertEqual(record['exit_code'], 9)
        self.assertEqual(record['verification']['exit_codes'], [9])
        self.assertTrue(record['undone'])
        self.assertIn('added: "artifact"', self.runner.invoke(main, ['diff']).output)

    def test_agent_failure_skips_validation(self):
        result = self.execute([python("from pathlib import Path; Path('marker').touch()")], source='raise SystemExit(5)')
        self.assertEqual(result.exit_code, 5)
        self.assertFalse(Path('marker').exists())
        self.assertEqual(core.records(Path.cwd())[-1]['verification']['state'], 'skipped')

    def test_missing_validator_is_failure_and_can_recover(self):
        result = self.execute(['/nonexistent/agentguard-validator'], rollback=True)
        self.assertEqual(result.exit_code, 127, result.output)
        self.assertEqual(Path('source').read_text(), 'before')

    def test_invalid_or_blocked_validator_prevents_agent_launch(self):
        for command in ('', '"', 'rm -rf marker'):
            with self.subTest(command=command), patch('agentguard.cli.subprocess.run') as run:
                result = self.execute([command])
                self.assertNotEqual(result.exit_code, 0)
                run.assert_not_called()
        self.assertFalse(Path('.agentguard/snapshots').exists())

    def test_failed_check_keeps_changes_by_default_and_hides_args(self):
        result = self.execute([python("raise SystemExit(8) # PRIVATE_TOKEN")])
        self.assertEqual(result.exit_code, 8, result.output)
        self.assertEqual(Path('source').read_text(), 'agent')
        self.assertNotIn('PRIVATE_TOKEN', self.runner.invoke(main, ['history', '--json']).output)
