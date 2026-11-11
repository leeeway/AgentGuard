import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core


def python(code):
    return shlex.join([sys.executable, '-c', code])


class RunProfiles(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('source').write_text('before')
        Path('.env').write_text('FAKE_TEST_SECRET')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def configure(self, **options):
        path = Path('.agentguard/config.toml')
        with path.open('a') as stream:
            stream.write('\n[profiles.ci]\n')
            for key, value in options.items():
                stream.write(key + ' = ' + json.dumps(value) + '\n')

    def execute(self, flags=(), code="from pathlib import Path; Path('source').write_text('agent')"):
        return self.runner.invoke(main, ['run'] + list(flags) + ['--', sys.executable, '-c', code])

    def test_profile_enables_protection_validation_and_rollback(self):
        self.configure(python_hooks=True, protect_files=True, rollback_on_failure=True,
                       verify=[python('raise SystemExit(6)')], verify_timeout=5)
        Path('worker.py').write_text("""from pathlib import Path
Path('source').write_text('agent')
try: Path('.env').read_text()
except PermissionError: pass
else: raise AssertionError('profile protection inactive')
""")
        result = self.runner.invoke(main, ['run', '--profile', 'ci', '--', sys.executable, 'worker.py'])
        self.assertEqual(result.exit_code, 6, result.output)
        self.assertEqual(Path('source').read_text(), 'before')
        row = json.loads(self.runner.invoke(main, ['history', '--json']).output)[0]
        self.assertEqual(row['profile'], 'ci')
        self.assertEqual(row['hooks'], ['python'])
        self.assertTrue(row['file_protection'])
        self.assertEqual(row['automatic_rollback'], 'restored')
        self.assertEqual(row['verification']['timeout_seconds'], 5)

    def test_explicit_flags_override_booleans_and_replace_checks(self):
        self.configure(python_hooks=True, protect_files=True, rollback_on_failure=True,
                       verify=[python("from pathlib import Path; Path('profile_check').touch()")])
        result = self.execute(['--profile', 'ci', '--no-python-hooks', '--no-protect-files',
                               '--no-rollback-on-failure', '--verify', python('raise SystemExit(7)')])
        self.assertEqual(result.exit_code, 7, result.output)
        self.assertEqual(Path('source').read_text(), 'agent')
        self.assertFalse(Path('profile_check').exists())
        row = core.records(Path.cwd())[-1]
        self.assertEqual(row['hooks'], [])
        self.assertFalse(row['file_protection'])
        self.assertEqual(row['automatic_rollback'], 'disabled')
        self.assertEqual(row['verification']['requested'], 1)

    def test_no_verify_clears_profile_checks_and_timeout(self):
        self.configure(verify=[python('raise SystemExit(8)')], verify_timeout=1)
        result = self.execute(['--profile', 'ci', '--no-verify'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(core.records(Path.cwd())[-1]['verification']['state'], 'disabled')
        self.assertNotIn('timeout_seconds', core.records(Path.cwd())[-1]['verification'])
        for flags in (['--no-verify', '--verify', 'echo ok'], ['--no-verify', '--verify-timeout', '1']):
            with patch('agentguard.cli.subprocess.run') as run:
                result = self.execute(flags)
            self.assertEqual(result.exit_code, 2)
            run.assert_not_called()

    def test_profile_is_opt_in_and_positive_overrides_work(self):
        self.configure(python_hooks=False, protect_files=False, verify=[python('raise SystemExit(3)')], verify_timeout=1)
        result = self.execute()
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(core.records(Path.cwd())[-1]['profile'])
        result = self.execute(['--profile', 'ci', '--python-hooks', '--protect-files', '--verify-timeout', '2'])
        self.assertEqual(result.exit_code, 3)
        row = core.records(Path.cwd())[-1]
        self.assertEqual(row['hooks'], ['python'])
        self.assertTrue(row['file_protection'])
        self.assertEqual(row['verification']['timeout_seconds'], 2)

    def test_unknown_profile_and_invalid_combinations_do_not_start(self):
        self.configure(python_hooks=True, protect_files=True)
        for flags in (['--profile', 'missing'], ['--profile', 'ci', '--no-python-hooks']):
            with patch('agentguard.cli.subprocess.run') as run:
                result = self.execute(flags)
            self.assertNotEqual(result.exit_code, 0)
            run.assert_not_called()
        self.assertFalse(Path('.agentguard/snapshots').exists())

    def test_invalid_profile_schema_is_rejected_without_command_disclosure(self):
        cases = ['profiles = []', '[profiles]\nci = 1',
                 '[profiles.ci]\npython_hooks = "yes"', '[profiles.ci]\nverify = "command"',
                 '[profiles.ci]\nverify = [1]', '[profiles.ci]\nverify = ["\'PRIVATE_TOKEN"]',
                 '[profiles.ci]\nverify_timeout = true', '[profiles.ci]\nverify_timeout = nan',
                 '[profiles.ci]\nverify_timeout = -1', '[profiles.ci]\nunknown_flag = true',
                 '[profiles."invalid name"]\npython_hooks = true']
        for config in cases:
            with self.subTest(config=config):
                Path('.agentguard/config.toml').write_text(config)
                with patch('agentguard.cli.subprocess.run') as run:
                    result = self.execute()
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertNotIn('PRIVATE_TOKEN', result.output)
                run.assert_not_called()

    def test_list_profiles_and_preflight_saved_validation(self):
        self.assertEqual(self.runner.invoke(main, ['profiles', '--json']).output, '[]\n')
        self.configure(verify=['rm -rf PRIVATE_TOKEN'], rollback_on_failure=True)
        result = self.runner.invoke(main, ['profiles', '--json'])
        row = json.loads(result.output)[0]
        self.assertEqual(row['name'], 'ci')
        self.assertEqual(row['verification_count'], 1)
        self.assertNotIn('PRIVATE_TOKEN', result.output)
        with patch('agentguard.cli.subprocess.run') as run:
            result = self.execute(['--profile', 'ci'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Verification command 1 blocked', result.output)
        run.assert_not_called()
        self.assertFalse(Path('.agentguard/snapshots').exists())
