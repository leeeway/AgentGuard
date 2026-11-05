import os
from pathlib import Path
import sys
import unittest

from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core


class Sessions(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.assertEqual(self.runner.invoke(main, ['init']).exit_code, 0)

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def run_script(self, code):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', code])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_restore_uncommitted_files_deletions_modes_and_links(self):
        Path('source').write_text('uncommitted work')
        Path('source').chmod(0o755)
        Path('deleted').write_text('keep')
        Path('link').symlink_to('source')
        self.run_script("from pathlib import Path; Path('source').write_text('agent'); Path('source').chmod(0o644); Path('deleted').unlink(); Path('new').mkdir(); Path('new/file').write_text('new'); Path('link').unlink(); Path('link').symlink_to('deleted')")
        self.assertIn('modified: "source"', self.runner.invoke(main, ['status']).output)
        result = self.runner.invoke(main, ['undo', '--all'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('source').read_text(), 'uncommitted work')
        self.assertEqual(Path('source').stat().st_mode & 0o777, 0o755)
        self.assertEqual(Path('deleted').read_text(), 'keep')
        self.assertEqual(os.readlink('link'), 'source')
        self.assertFalse(Path('new').exists())

    def test_conflict_is_checked_before_any_changes(self):
        Path('a').write_text('before')
        self.run_script("from pathlib import Path; Path('a').write_text('agent'); Path('b').write_text('new')")
        Path('a').write_text('human')
        result = self.runner.invoke(main, ['undo'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(Path('a').read_text(), 'human')
        self.assertTrue(Path('b').exists())

    def test_new_user_file_in_created_directory_survives(self):
        self.run_script("from pathlib import Path; Path('new').mkdir()")
        Path('new/human').write_text('work')
        self.assertNotEqual(self.runner.invoke(main, ['undo']).exit_code, 0)
        self.assertEqual(Path('new/human').read_text(), 'work')

    def test_replaced_parent_link_blocks_restore(self):
        Path('parent').mkdir()
        Path('parent/deleted').write_text('original')
        self.run_script("from pathlib import Path; Path('parent/deleted').unlink()")
        Path('parent').rmdir()
        Path('outside').mkdir()
        Path('parent').symlink_to('outside', target_is_directory=True)
        result = self.runner.invoke(main, ['undo'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(Path('outside/deleted').exists())

    def test_snapshot_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe path'):
            core.restore(Path.cwd(), {'before': {'../escape': {}}, 'after': {}})

    def test_block_does_not_execute_or_snapshot(self):
        Path('keep').write_text('work')
        result = self.runner.invoke(main, ['run', '--', 'rm', '-rf', 'keep'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(Path('keep').exists())
        self.assertEqual(core.records(Path.cwd()), [])

    def test_exit_code_and_snapshot_on_failure(self):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'raise SystemExit(7)'])
        self.assertEqual(result.exit_code, 7)
        self.assertIn('after', core.records(Path.cwd())[0])

    def test_init_preserves_config_and_git(self):
        path = Path('.agentguard/config.toml')
        path.write_text('# custom\n')
        self.runner.invoke(main, ['init'])
        self.assertEqual(path.read_text(), '# custom\n')
        Path('.git').mkdir()
        Path('.git/HEAD').write_text('unchanged')
        self.run_script("from pathlib import Path; Path('new').touch()")
        self.runner.invoke(main, ['undo'])
        self.assertEqual(Path('.git/HEAD').read_text(), 'unchanged')

    def test_lock_rejects_parallel_run(self):
        Path('.agentguard/session.lock').touch()
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(core.records(Path.cwd()), [])


if __name__ == '__main__':
    unittest.main()
