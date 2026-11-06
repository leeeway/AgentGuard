import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from click.testing import CliRunner
from agentguard import core
from agentguard.cli import main


class Exclusions(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def configure(self, patterns):
        Path('.agentguard/config.toml').write_text('[snapshots]\nexclude = ' + json.dumps(patterns) + '\nmax_total_bytes = 100')

    def execute(self, source):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', source])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_directory_pruned_and_not_counted_in_budget(self):
        self.configure(['node_modules'])
        Path('node_modules').mkdir()
        Path('node_modules/big').write_bytes(b'x' * 1000)
        Path('source').write_text('small')
        original = Path.iterdir
        def guarded(path):
            if path.name == 'node_modules':
                raise AssertionError('excluded directory traversed')
            return original(path)
        with patch.object(Path, 'iterdir', guarded):
            plan = self.runner.invoke(main, ['snapshot-plan', '--json'])
            self.assertEqual(plan.exit_code, 0, plan.output)
            self.assertEqual(json.loads(plan.output)['total_bytes'], 5)
            self.assertEqual(list(core.capture(Path.cwd(), core.config(Path.cwd()))), ['source'])

    def test_undo_uses_original_scope_after_config_changes(self):
        self.configure(['cache'])
        Path('source').write_text('before')
        Path('cache').write_text('before cache')
        self.execute("from pathlib import Path; Path('source').write_text('after'); Path('cache').write_text('after cache')")
        self.configure(['source'])
        result = self.runner.invoke(main, ['undo'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('source').read_text(), 'before')
        self.assertEqual(Path('cache').read_text(), 'after cache')
        self.assertEqual(core.records(Path.cwd())[-1]['snapshot_excludes'], ['cache'])

    def test_new_directory_with_excluded_child_blocks_entire_undo(self):
        self.configure(['new/cache'])
        Path('source').write_text('before')
        self.execute("from pathlib import Path; Path('source').write_text('after'); Path('new').mkdir(); Path('new/cache').write_text('excluded')")
        result = self.runner.invoke(main, ['undo'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('new/cache', result.output)
        self.assertEqual(Path('source').read_text(), 'after')
        self.assertTrue(Path('new/cache').exists())

    def test_glob_and_existing_directory(self):
        self.configure(['*.log'])
        Path('d').mkdir()
        Path('d/a.log').write_text('old log')
        Path('d/source').write_text('before')
        self.execute("from pathlib import Path; Path('d/a.log').write_text('new log'); Path('d/source').unlink()")
        result = self.runner.invoke(main, ['undo'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('d/source').read_text(), 'before')
        self.assertEqual(Path('d/a.log').read_text(), 'new log')

    def test_invalid_patterns_fail_before_snapshot(self):
        for patterns in ('node_modules', [''], ['../outside'], ['/abs'], ['dir/'], [3]):
            self.configure(patterns)
            result = self.runner.invoke(main, ['snapshot-plan'])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn('exclusions', result.output)
