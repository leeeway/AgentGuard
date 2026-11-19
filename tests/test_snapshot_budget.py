import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard import core
from agentguard.cli import main


class SnapshotBudget(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def limits(self, text):
        Path('.agentguard/config.toml').write_text('[snapshots]\n' + text)

    def test_plan_counts_without_reading_contents(self):
        Path('dir').mkdir()
        Path('dir/a').write_bytes(b'12345')
        Path('link').symlink_to('dir', target_is_directory=True)
        Path('.git').mkdir()
        Path('.git/ignored').write_bytes(b'x' * 100)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
            result = self.runner.invoke(main, ['snapshot-plan', '--json'])
        self.assertEqual(result.exit_code, 0, result.output)
        row = json.loads(result.output)
        self.assertEqual((row['files'], row['directories'], row['symlinks'], row['total_bytes']), (1, 1, 1, 5))
        self.assertFalse(Path('.agentguard/snapshots').exists())

    def test_file_limit_blocks_before_execution(self):
        self.limits('max_file_bytes = 4')
        Path('a').write_text('12345')
        plan = self.runner.invoke(main, ['snapshot-plan', '--json'])
        self.assertEqual(plan.exit_code, 3)
        self.assertEqual(json.loads(plan.output)['issues'][0]['reason'], 'max_file_bytes')
        with patch('agentguard.cli.subprocess.run') as run:
            result = self.runner.invoke(main, ['run', '--', 'echo', 'hello'])
        run.assert_not_called()
        self.assertIn('budget exceeded', result.output)
        self.assertFalse(Path('.agentguard/session.lock').exists())

    def test_total_limit_and_exact_boundary(self):
        self.limits('max_total_bytes = 6\nmax_file_bytes = 4')
        Path('a').write_text('123')
        Path('b').write_text('456')
        self.assertEqual(len(core.capture(Path.cwd(), core.config(Path.cwd()))), 2)
        Path('b').write_text('4567')
        self.assertEqual(self.runner.invoke(main, ['snapshot-plan']).exit_code, 3)
        with self.assertRaisesRegex(ValueError, 'budget exceeded'):
            core.capture(Path.cwd(), core.config(Path.cwd()))

    def test_recover_incomplete_snapshot_after_post_run_limit(self):
        self.limits('max_total_bytes = 4\nexclude = ["cache"]')
        Path('a').write_text('old')
        source = "from pathlib import Path; Path('a').write_text('12345'); Path('cache').write_text('unsnapshotted'); raise SystemExit(7)"
        result = self.runner.invoke(main, ['run', '--rollback-on-failure', '--', sys.executable, '-c', source])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('budget exceeded', result.output)
        record = core.records(Path.cwd())[0]
        self.assertNotIn('after', record)
        manifest = Path('.agentguard/snapshots') / (record['id'] + '.json')
        original = manifest.read_bytes()
        blocked = self.runner.invoke(main, ['undo'])
        self.assertNotEqual(blocked.exit_code, 0)
        self.assertIn('recover --dry-run', blocked.output)
        blocked = self.runner.invoke(main, ['recover'])
        self.assertNotEqual(blocked.exit_code, 0)
        self.assertIn('budget exceeded', blocked.output)
        self.assertEqual(manifest.read_bytes(), original)

        self.limits('max_total_bytes = 5\nexclude = ["a"]')
        preview = self.runner.invoke(main, ['recover', '--dry-run'])
        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertIn('modified: "a"', preview.output)
        self.assertNotIn('cache', preview.output)
        self.assertNotIn('12345', preview.output)
        self.assertEqual(manifest.read_bytes(), original)

        result = self.runner.invoke(main, ['recover', '--session', record['id']])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('a').read_text(), '12345')
        history = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertEqual(history['id'], record['id'])
        self.assertEqual(history['state'], 'complete')
        self.assertTrue(history['recovered_at'])
        self.assertIsNone(history['finished_at'])
        self.assertIsNone(history['exit_code'])
        self.assertEqual(history['automatic_rollback'], 'pending')

        Path('a').write_text('edit after recovery')
        blocked = self.runner.invoke(main, ['undo'])
        self.assertIn('Undo refused', blocked.output)
        self.assertEqual(Path('a').read_text(), 'edit after recovery')
        Path('a').write_text('12345')
        result = self.runner.invoke(main, ['undo'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('a').read_text(), 'old')
        self.assertEqual(Path('cache').read_text(), 'unsnapshotted')
        report = self.runner.invoke(main, ['doctor', '--json'])
        codes = {check['code'] for check in json.loads(report.output)['checks']}
        self.assertNotIn('incomplete-session', codes)
        self.assertNotIn('unfinished-recovery', codes)

    def test_invalid_limits(self):
        for value in ('0', '-1', 'true', '"100"'):
            self.limits('max_total_bytes = ' + value)
            result = self.runner.invoke(main, ['snapshot-plan'])
            self.assertEqual(result.exit_code, 1)
            self.assertIn('positive integer', result.output)
