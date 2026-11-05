import json
from pathlib import Path
import sys
import unittest

from click.testing import CliRunner
from agentguard import core, validation
from agentguard.cli import main


class SnapshotIntegrity(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def session(self, source):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', source])
        self.assertEqual(result.exit_code, 0, result.output)
        return core.records(Path.cwd())[-1]

    def test_dry_run_preserves_files_and_history(self):
        record = self.session("from pathlib import Path; Path('a').write_text('agent')")
        result = self.runner.invoke(main, ['undo', '--dry-run'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('would restore: "a"', result.output)
        self.assertEqual(Path('a').read_text(), 'agent')
        self.assertEqual(core.records(Path.cwd())[-1], record)
        Path('a').write_text('human')
        self.assertNotEqual(self.runner.invoke(main, ['undo', '--dry-run']).exit_code, 0)
        self.assertEqual(Path('a').read_text(), 'human')

    def test_corrupt_content_rejected_before_any_deletion(self):
        Path('a').write_text('original')
        record = self.session("from pathlib import Path; Path('a').write_text('agent'); Path('b').touch()")
        record['before']['a']['data'] = 'not base64!'
        with self.assertRaisesRegex(ValueError, 'file content'):
            core.restore(Path.cwd(), record)
        self.assertEqual(Path('a').read_text(), 'agent')
        self.assertTrue(Path('b').exists())

    def test_invalid_record_shapes(self):
        record = {'id': '0' * 20 + '-' + 'a' * 32, 'before': {}}
        cases = [None, {'before': []}, dict(record, id='../escape'),
                 dict(record, before={'a': {'kind': 'device'}}),
                 dict(record, before={'a': {'kind': 'file', 'mode': -1, 'data': ''}}),
                 dict(record, before={'a/b': {'kind': 'dir', 'mode': 0o755}}),
                 dict(record, before={'a': {'kind': 'link', 'target': ''}})]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validation.record(value)

    def test_mismatched_record_id_is_reported_cleanly(self):
        record = self.session('pass')
        path = Path('.agentguard/snapshots') / (record['id'] + '.json')
        record['id'] = '0' * 20 + '-' + 'b' * 32
        path.write_text(json.dumps(record))
        result = self.runner.invoke(main, ['history'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('filename does not match', result.output)

    def test_snapshot_directory_link_rejected(self):
        Path('outside').mkdir()
        Path('.agentguard/snapshots').symlink_to(Path('outside').resolve(), target_is_directory=True)
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(list(Path('outside').iterdir()), [])

    def test_file_directory_type_transitions(self):
        Path('file').write_text('original')
        Path('dir').mkdir()
        Path('dir/child').write_text('child')
        self.session("from pathlib import Path; Path('file').unlink(); Path('file').mkdir(); Path('file/new').touch(); Path('dir/child').unlink(); Path('dir').rmdir(); Path('dir').write_text('replacement')")
        result = self.runner.invoke(main, ['undo'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('file').read_text(), 'original')
        self.assertEqual(Path('dir/child').read_text(), 'child')

    def test_read_only_directory_restored_after_children(self):
        Path('d').mkdir()
        Path('d/a').write_text('original')
        Path('d').chmod(0o555)
        try:
            self.session("from pathlib import Path; Path('d').chmod(0o755); Path('d/a').unlink(); Path('d').rmdir()")
            result = self.runner.invoke(main, ['undo'])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(Path('d/a').read_text(), 'original')
            self.assertEqual(Path('d').stat().st_mode & 0o777, 0o555)
        finally:
            if Path('d').exists():
                Path('d').chmod(0o755)
