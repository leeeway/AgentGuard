import json
from pathlib import Path
import sys
import unittest
from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core, objects


class Doctor(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def report(self):
        response = self.runner.invoke(main, ['doctor', '--json'])
        return response.exit_code, json.loads(response.output)

    def test_uninitialized_does_not_create_files(self):
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertGreater(report['errors'], 0)
        self.assertFalse(Path('.agentguard').exists())

    def test_clean_workspace(self):
        self.runner.invoke(main, ['init'])
        self.assertEqual(self.report()[0], 0)

    def test_lock_and_empty_policy_warn_without_repair(self):
        self.runner.invoke(main, ['init'])
        Path('.agentguard/config.toml').write_text('')
        Path('.agentguard/session.lock').touch()
        code, report = self.report()
        self.assertEqual(code, 3)
        self.assertEqual(report['warnings'], 2)
        self.assertTrue(Path('.agentguard/session.lock').exists())

    def test_reports_multiple_corrupt_records_and_valid_session(self):
        self.runner.invoke(main, ['init'])
        self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        folder = Path('.agentguard/snapshots')
        (folder / 'broken-one.json').write_text('{')
        (folder / 'broken-two.json').write_text('[]')
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report['errors'], 2)
        self.assertTrue(any(c['code'] == 'snapshot-integrity' and c['level'] == 'ok' for c in report['checks']))
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_incomplete_session_is_warning(self):
        self.runner.invoke(main, ['init'])
        core.save(Path.cwd(), {'id': '0' * 20 + '-' + 'a' * 32, 'before': {}})
        code, report = self.report()
        self.assertEqual(code, 3)
        self.assertTrue(any(c['code'] == 'incomplete-session' for c in report['checks']))

    def test_checksum_error_without_disclosing_contents(self):
        self.runner.invoke(main, ['init'])
        Path('secret').write_text('PRIVATE_VALUE')
        self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        (Path('.agentguard/objects') / objects.digest(b'PRIVATE_VALUE')).write_bytes(b'CORRUPT_VALUE')
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertNotIn('PRIVATE_VALUE', json.dumps(report))
        self.assertNotIn('CORRUPT_VALUE', json.dumps(report))

    def test_storage_file_instead_of_directory(self):
        self.runner.invoke(main, ['init'])
        Path('.agentguard/snapshots').touch()
        Path('.agentguard/objects').touch()
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report['errors'], 2)
