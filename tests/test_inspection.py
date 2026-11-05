import json
from pathlib import Path
import sys
import unittest

from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core


class Inspection(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def run_script(self, script, code=0):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', script])
        self.assertEqual(result.exit_code, code, result.output)

    def test_history_metadata_and_undone_state(self):
        self.run_script("from pathlib import Path; Path('a').write_text('secret'); raise SystemExit(7)", 7)
        result = self.runner.invoke(main, ['history', '--json'])
        row = json.loads(result.output)[0]
        self.assertEqual(row['exit_code'], 7)
        self.assertEqual(row['changed_paths'], 1)
        self.assertTrue(row['started_at'])
        self.assertTrue(row['finished_at'])
        self.assertNotIn('secret', result.output)
        self.runner.invoke(main, ['undo'])
        self.assertEqual(json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]['state'], 'undone')

    def test_diff_contents_are_opt_in_and_use_recorded_data(self):
        Path('a').write_text('before\n')
        self.run_script("from pathlib import Path; Path('a').write_text('SECRET_AFTER\\n')")
        Path('a').write_text('human later')
        summary = self.runner.invoke(main, ['diff'])
        self.assertIn('modified: "a"', summary.output)
        self.assertNotIn('SECRET_AFTER', summary.output)
        diff = self.runner.invoke(main, ['diff', '--contents'])
        self.assertIn('-before', diff.output)
        self.assertIn('+SECRET_AFTER', diff.output)
        self.assertNotIn('human later', diff.output)

    def test_session_selection_and_path_filter(self):
        self.run_script("from pathlib import Path; Path('a').write_text('one'); Path('b').write_text('two')")
        first = core.records(Path.cwd())[0]['id']
        self.run_script("from pathlib import Path; Path('c').touch()")
        result = self.runner.invoke(main, ['diff', '--session', first, '--path', 'a'])
        self.assertEqual(result.output, 'added: "a"\n')
        self.assertNotEqual(self.runner.invoke(main, ['diff', '--session', 'missing']).exit_code, 0)
        self.assertNotEqual(self.runner.invoke(main, ['diff', '--path', 'missing']).exit_code, 0)
        self.assertEqual(len(json.loads(self.runner.invoke(main, ['history', '--json', '--limit', '1']).output)), 1)

    def test_binary_and_terminal_controls(self):
        self.run_script("from pathlib import Path; Path('binary').write_bytes(b'\\x00abc'); Path('text').write_text('\\x1b[31mred')")
        result = self.runner.invoke(main, ['diff', '--contents'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('Binary content omitted', result.output)
        self.assertNotIn('\x1b', result.output)
        self.assertIn('No newline at end of file', result.output)

    def test_legacy_and_incomplete_sessions(self):
        record = {'id': '00000000000000000001-' + 'a' * 32, 'before': {}}
        core.save(Path.cwd(), record)
        row = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertEqual(row['state'], 'incomplete')
        self.assertIsNone(row['exit_code'])
        self.assertNotEqual(self.runner.invoke(main, ['diff']).exit_code, 0)

    def test_empty_history(self):
        self.assertEqual(self.runner.invoke(main, ['history', '--json']).output, '[]\n')
        self.assertNotEqual(self.runner.invoke(main, ['diff']).exit_code, 0)
