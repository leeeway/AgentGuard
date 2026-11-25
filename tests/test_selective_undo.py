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
        record = core.records(Path.cwd())[0]
        Path('b').write_text('human')
        result = self.runner.invoke(main, ['undo', '--dry-run', '--json', '--path', 'a', '--path', 'b'])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(json.loads(result.output), {
            'operation': 'undo', 'session': record['id'], 'dry_run': True,
            'paths': ['a', 'b'], 'conflicts': ['b'],
        })
        self.assertEqual(Path('a').read_text(), 'agent a')
        self.assertEqual(core.records(Path.cwd())[0], record)
        # Conflict on an unselected path does not block restoration of a.
        self.assertEqual(self.runner.invoke(main, ['undo', '--path', 'a']).exit_code, 0)
        self.assertEqual(Path('b').read_text(), 'human')

    def test_undo_session_guard_rejects_a_preview_after_a_new_session(self):
        self.execute()
        preview = self.runner.invoke(main, ['undo', '--dry-run', '--json'])
        self.assertEqual(preview.exit_code, 0, preview.output)
        first = json.loads(preview.output)
        self.assertEqual(first['paths'], ['a', 'b'])
        self.assertEqual(first['conflicts'], [])

        self.execute("from pathlib import Path; Path('new').touch()")
        latest = core.records(Path.cwd())[-1]['id']
        stale = self.runner.invoke(main, ['undo', '--session', first['session']])
        self.assertEqual(stale.exit_code, 1, stale.output)
        self.assertIn('not the latest non-undone session', stale.output)
        self.assertTrue(Path('new').exists())
        self.assertEqual(Path('a').read_text(), 'agent a')

        result = self.runner.invoke(main, ['undo', '--session', latest, '--json'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), {
            'operation': 'undo', 'session': latest, 'dry_run': False, 'paths': ['new'], 'conflicts': [],
        })
        self.assertFalse(Path('new').exists())
        self.assertEqual(Path('a').read_text(), 'agent a')

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

    def test_unknown_subtree_under_nested_directory_blocks_full_undo(self):
        self.execute("from pathlib import Path; Path('a').write_text('agent a'); "
                     "Path('d/nested').mkdir(parents=True); Path('d/nested/created').touch()")
        Path('d/nested/extra').mkdir()
        Path('d/nested/extra/keep').write_text('later edit')

        result = self.runner.invoke(main, ['undo', '--json'])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(json.loads(result.output)['conflicts'], ['d/nested/extra'])
        self.assertEqual(Path('a').read_text(), 'agent a')
        self.assertTrue(Path('d/nested/created').exists())
        self.assertEqual(Path('d/nested/extra/keep').read_text(), 'later edit')

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
