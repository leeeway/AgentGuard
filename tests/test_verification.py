import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch
from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core, objects


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

    def execute(self, validators, source="from pathlib import Path; Path('source').write_text('agent')",
                rollback=False, checkpoint=False):
        flags = ['--rollback-on-failure'] if rollback else []
        if checkpoint:
            flags.append('--verify-checkpoint')
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
        result = self.execute([python("from pathlib import Path; Path('artifact').touch(); raise SystemExit(9)"), python("from pathlib import Path; Path('skipped').touch()")], rollback=True, checkpoint=True)
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
        self.assertEqual(self.runner.invoke(main, ['diff', '--phase', 'agent']).output,
                         'modified: "source"\n')
        self.assertEqual(self.runner.invoke(main, ['diff', '--phase', 'verification']).output,
                         'added: "artifact"\n')

    def test_agent_failure_skips_validation(self):
        result = self.execute([python("from pathlib import Path; Path('marker').touch()")],
                              source='raise SystemExit(5)', checkpoint=True)
        self.assertEqual(result.exit_code, 5)
        self.assertFalse(Path('marker').exists())
        self.assertEqual(core.records(Path.cwd())[-1]['verification']['state'], 'skipped')
        diff = self.runner.invoke(main, ['diff', '--phase', 'agent'])
        self.assertEqual(diff.exit_code, 1, diff.output)
        self.assertIn('No verification checkpoint', diff.output)

    def test_phase_diff_and_gc_keep_changes_visible_only_at_checkpoint(self):
        validator = """
import json
from pathlib import Path

snapshot = next(Path('.agentguard/snapshots').glob('*.json'))
record = json.loads(snapshot.read_text())
assert record['format_version'] == 3
assert record['verification']['state'] == 'pending'
assert 'source' in record['before_verification'] and 'after' not in record
Path('source').write_text('before')
Path('artifact').touch()
"""
        result = self.execute([python(validator)], checkpoint=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.runner.invoke(main, ['diff']).output, 'added: "artifact"\n')
        gc = self.runner.invoke(main, ['gc'])
        self.assertEqual(gc.exit_code, 0, gc.output)
        phase = self.runner.invoke(main, ['diff', '--phase', 'agent', '--contents', '--path', 'source'])
        self.assertEqual(phase.exit_code, 0, phase.output)
        self.assertIn('+agent', phase.output)
        phase = self.runner.invoke(main, ['diff', '--phase', 'verification', '--contents', '--path', 'source'])
        self.assertEqual(phase.exit_code, 0, phase.output)
        self.assertIn('+before', phase.output)

        row = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertTrue(row['verification_checkpoint'])
        result = self.runner.invoke(main, ['diff', '--phase', 'verification', '--json', '--path', 'source'])
        stage = json.loads(result.output)
        self.assertEqual(stage['session'], row['id'])
        self.assertEqual(stage['phase'], 'verification')
        self.assertEqual([(change['path'], change['change']) for change in stage['changes']],
                         [('source', 'modified')])
        object_path = objects.directory(Path.cwd()) / objects.digest(b'agent')
        object_path.write_bytes(b'corrupted checkpoint')
        report = self.runner.invoke(main, ['doctor', '--json'])
        self.assertEqual(report.exit_code, 1, report.output)
        self.assertIn('checksum mismatch', report.output)

    def test_checkpoint_budget_failure_stops_before_running_verification(self):
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text() + '\nmax_file_bytes = 6\n')
        result = self.execute([python("from pathlib import Path; Path('marker').touch()")],
                              source="from pathlib import Path; Path('source').write_text('over budget')",
                              checkpoint=True)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn('Could not save verification checkpoint', result.output)
        self.assertFalse(Path('marker').exists())
        record = core.records(Path.cwd())[0]
        self.assertNotIn('after', record)
        self.assertNotIn('before_verification', record)

    def test_checkpoint_survives_incomplete_final_snapshot_and_recovery(self):
        config = Path('.agentguard/config.toml')
        default = config.read_text()
        config.write_text(default + '\nmax_file_bytes = 6\n')
        result = self.execute([python("from pathlib import Path; Path('artifact').write_text('over budget')")],
                              checkpoint=True)
        self.assertIn('budget exceeded', result.output)
        self.assertEqual(self.runner.invoke(main, ['diff', '--phase', 'agent']).output,
                         'modified: "source"\n')
        self.assertIn('incomplete', self.runner.invoke(main, ['diff', '--phase', 'verification']).output)
        result = self.runner.invoke(main, ['show', '--snapshot', 'before-verification', 'source'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, b'agent')
        result = self.runner.invoke(main, ['show', 'source'])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn('after snapshot is unavailable', result.output)
        config.write_text(default)
        recovered = self.runner.invoke(main, ['recover'])
        self.assertEqual(recovered.exit_code, 0, recovered.output)
        self.assertIn('unavailable after recovery',
                      self.runner.invoke(main, ['diff', '--phase', 'verification']).output)

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
