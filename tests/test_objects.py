import base64
import json
from pathlib import Path
import sys
import unittest

from click.testing import CliRunner
from agentguard import core, objects
from agentguard.cli import main


class ObjectStore(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def run_script(self, code='pass'):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', code])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_identical_files_and_sessions_share_one_object(self):
        Path('a').write_bytes(b'x' * 100000)
        Path('b').write_bytes(b'x' * 100000)
        self.run_script()
        self.run_script()
        files = list(Path('.agentguard/objects').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_size, 100000)
        for path in Path('.agentguard/snapshots').glob('*.json'):
            record = json.loads(path.read_text())
            self.assertEqual(record['format_version'], 2)
            self.assertNotIn('data', record['before']['a'])
            self.assertLess(path.stat().st_size, 3000)

    def test_legacy_record_loads_and_restores(self):
        record = {'id': '0' * 20 + '-' + 'a' * 32,
                  'before': {'a': {'kind': 'file', 'mode': 0o644, 'data': base64.b64encode(b'legacy').decode()}},
                  'after': {}}
        folder = Path('.agentguard/snapshots')
        folder.mkdir()
        (folder / (record['id'] + '.json')).write_text(json.dumps(record))
        result = self.runner.invoke(main, ['undo'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('a').read_text(), 'legacy')
        self.assertTrue(core.records(Path.cwd())[0]['undone'])

    def test_gc_after_retention_preserves_referenced_content(self):
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('max_history = 30', 'max_history = 1'))
        Path('a').write_text('first')
        self.run_script("from pathlib import Path; Path('a').write_text('second')")
        self.run_script("from pathlib import Path; Path('a').write_text('third')")
        self.assertEqual(len(core.records(Path.cwd())), 1)
        self.assertEqual(objects.collect(Path.cwd()), (1, 5))
        result = self.runner.invoke(main, ['gc', '--dry-run'])
        self.assertIn('Would remove 1', result.output)
        self.assertEqual(len(list(Path('.agentguard/objects').iterdir())), 3)
        self.assertEqual(self.runner.invoke(main, ['gc']).exit_code, 0)
        self.assertEqual(len(list(Path('.agentguard/objects').iterdir())), 2)
        self.assertEqual(self.runner.invoke(main, ['undo']).exit_code, 0)
        self.assertEqual(Path('a').read_text(), 'second')

    def test_corrupt_object_prevents_undo_and_gc(self):
        Path('a').write_text('before')
        self.run_script("from pathlib import Path; Path('a').write_text('after')")
        unused = objects.write(Path.cwd(), b'unreferenced')
        (Path('.agentguard/objects') / objects.digest(b'before')).write_bytes(b'corrupt')
        for command in (['undo'], ['gc']):
            result = self.runner.invoke(main, command)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('checksum mismatch', result.output)
        self.assertEqual(Path('a').read_text(), 'after')
        self.assertTrue((Path('.agentguard/objects') / unused).exists())

    def test_missing_and_symlink_objects_fail_closed(self):
        Path('a').write_text('content')
        self.run_script()
        path = Path('.agentguard/objects') / objects.digest(b'content')
        path.unlink()
        self.assertNotEqual(self.runner.invoke(main, ['history']).exit_code, 0)
        path.symlink_to(Path('a').resolve())
        self.assertNotEqual(self.runner.invoke(main, ['history']).exit_code, 0)

    def test_gc_respects_session_lock(self):
        name = objects.write(Path.cwd(), b'unused')
        Path('.agentguard/session.lock').touch()
        self.assertNotEqual(self.runner.invoke(main, ['gc']).exit_code, 0)
        self.assertTrue((Path('.agentguard/objects') / name).exists())
