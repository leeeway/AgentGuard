import json
from pathlib import Path
import sys
import unittest
from click.testing import CliRunner
from agentguard import core
from agentguard.cli import main


class SelectiveUndo(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('a').write_text('before a')
        Path('b').write_text('before b')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, code="from pathlib import Path; Path('a').write_text('agent a'); Path('b').write_text('agent b')"):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', code])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_partial_then_full_preserves_later_edit_to_restored_file(self):
        self.execute()
        result = self.runner.invoke(main, ['undo', '--path', 'a'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('a').read_text(), 'before a')
        self.assertEqual(Path('b').read_text(), 'agent b')
        summary = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertEqual(summary['state'], 'partially-undone')
        self.assertEqual(summary['remaining_paths'], 1)
        self.assertNotIn('"a"', self.runner.invoke(main, ['status']).output)
        self.assertIn('modified: "a"', self.runner.invoke(main, ['diff']).output)
        Path('a').write_text('later human edit')
        self.assertEqual(self.runner.invoke(main, ['undo']).exit_code, 0)
        self.assertEqual(Path('a').read_text(), 'later human edit')
        self.assertEqual(Path('b').read_text(), 'before b')
        self.assertTrue(core.records(Path.cwd())[0]['undone'])

    def test_selected_paths_preflight_as_a_group(self):
        self.execute()
        Path('b').write_text('human')
        result = self.runner.invoke(main, ['undo', '--path', 'a', '--path', 'b'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(Path('a').read_text(), 'agent a')
        # Conflict on an unselected path does not block restoration of a.
        self.assertEqual(self.runner.invoke(main, ['undo', '--path', 'a']).exit_code, 0)
        self.assertEqual(Path('b').read_text(), 'human')

    def test_dry_run_and_invalid_selections(self):
        self.execute()
        record = core.records(Path.cwd())[0]
        self.assertEqual(self.runner.invoke(main, ['undo', '--path', 'a', '--dry-run']).exit_code, 0)
        self.assertEqual(core.records(Path.cwd())[0], record)
        for args in (['--path', '../escape'], ['--all', '--path', 'a'], ['--path', 'missing']):
            self.assertNotEqual(self.runner.invoke(main, ['undo'] + args).exit_code, 0)
        self.assertEqual(Path('a').read_text(), 'agent a')

    def test_created_files_and_links_then_directory_cleanup(self):
        self.execute("from pathlib import Path; Path('d').mkdir(); Path('d/new').touch(); Path('link').symlink_to('a')")
        result = self.runner.invoke(main, ['undo', '--path', 'd/new', '--path', 'link'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(Path('d').is_dir())
        self.assertFalse(Path('link').is_symlink())
        self.assertEqual(self.runner.invoke(main, ['undo']).exit_code, 0)
        self.assertFalse(Path('d').exists())

    def test_directory_and_missing_parent_require_full_undo(self):
        Path('d').mkdir()
        Path('d/deleted').write_text('original')
        self.execute("from pathlib import Path; Path('d/deleted').unlink(); Path('d').rmdir()")
        for path in ('d', 'd/deleted'):
            self.assertNotEqual(self.runner.invoke(main, ['undo', '--path', path]).exit_code, 0)
        self.assertEqual(self.runner.invoke(main, ['undo']).exit_code, 0)
        self.assertEqual(Path('d/deleted').read_text(), 'original')

    def test_repeated_restore_rejected_without_skipping_session(self):
        self.execute()
        self.runner.invoke(main, ['undo', '--path', 'a'])
        result = self.runner.invoke(main, ['undo', '--path', 'a'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('already restored', result.output)
        self.assertEqual(Path('b').read_text(), 'agent b')
