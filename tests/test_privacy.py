import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core, runtime
from agentguard.privacy import FilePolicy


class ProtectedFiles(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('.env').write_text('PRIVATE_TEST_SECRET')
        Path('public.txt').write_text('public')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, code, protected=True, env=None, flags=()):
        Path('worker.py').write_text(code)
        options = ['--python-hooks'] + (['--protect-files'] if protected else [])
        return self.runner.invoke(main, ['run'] + options + list(flags) + ['--', sys.executable, 'worker.py'], env=env)

    def test_common_open_apis_block_without_disclosing_path_or_content(self):
        result = self.execute("""from pathlib import Path
import os, io
for action in [lambda: open('.env'), lambda: Path('.env').read_text(),
               lambda: io.open('.env', 'rb'), lambda: os.open(b'.env', os.O_RDONLY),
               lambda: open('.env', 'r+'), lambda: open('.env', 'w')]:
    try: action()
    except PermissionError as e:
        assert '.env' not in str(e) and 'PRIVATE_TEST_SECRET' not in str(e)
    else: raise AssertionError('protected open succeeded')
assert Path('public.txt').read_text() == 'public'
Path('normal_output').write_text('allowed')
""")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('.env').read_text(), 'PRIVATE_TEST_SECRET')
        self.assertEqual(Path('normal_output').read_text(), 'allowed')
        logs = self.runner.invoke(main, ['audit', '--json'])
        rows = json.loads(logs.output)
        blocked = [row for row in rows if row['event'] == 'file.open']
        self.assertEqual(len(blocked), 6)
        self.assertTrue(all(row['decision'] == 'blocked' for row in blocked))
        self.assertNotIn('PRIVATE_TEST_SECRET', logs.output)
        self.assertNotIn('.env', logs.output)
        self.assertTrue(core.records(Path.cwd())[0]['file_protection'])

    def test_symlink_alias_and_chdir_keep_original_workspace_scope(self):
        Path('alias').symlink_to('.env')
        Path('sub').mkdir()
        Path('sub/.env.dev').write_text('nested')
        result = self.execute("""import os
from pathlib import Path
for path in ['alias', './sub/../.env']:
    try: Path(path).read_bytes()
    except PermissionError: pass
    else: raise AssertionError('alias not blocked')
os.chdir('sub')
try: Path('.env.dev').read_bytes()
except PermissionError: pass
else: raise AssertionError('nested file not blocked')
""")
        self.assertEqual(result.exit_code, 0, result.output)

    def test_home_directory_rule_uses_child_home(self):
        fake_home = Path('fake_home').resolve()
        (fake_home / '.ssh').mkdir(parents=True)
        (fake_home / '.ssh/id_test').write_text('FAKE_KEY')
        result = self.execute("""from pathlib import Path
try: (Path.home() / '.ssh/id_test').read_text()
except PermissionError: pass
else: raise AssertionError('home key not blocked')
""", env={'HOME': str(fake_home)})
        self.assertEqual(result.exit_code, 0, result.output)

    def test_explicit_policy_and_opt_in(self):
        config = Path('.agentguard/config.toml')
        config.write_text('[safety]\ndangerous_commands = "warn"\n[privacy]\nprotected_files = ["public.txt"]\n')
        result = self.execute("""from pathlib import Path
assert Path('.env').read_text() == 'PRIVATE_TEST_SECRET'
try: Path('public.txt').read_text()
except PermissionError: pass
else: raise AssertionError('custom rule ignored')
""")
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.execute("from pathlib import Path; assert Path('public.txt').read_text() == 'public'", protected=False)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_descendant_and_verification_inherit_protection(self):
        Path('child.py').write_text("""from pathlib import Path
try: Path('.env').read_text()
except PermissionError: pass
else: raise AssertionError('unguarded descendant')
""")
        verify = shlex.join([sys.executable, 'child.py'])
        result = self.execute("import subprocess,sys; subprocess.run([sys.executable, 'child.py'], check=True)", flags=['--verify', verify])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len([row for row in json.loads(self.runner.invoke(main, ['audit', '--json']).output) if row['event'] == 'file.open']), 2)

    def test_unhandled_denial_triggers_failure_rollback(self):
        result = self.execute("from pathlib import Path; Path('created').touch(); Path('.env').read_bytes()", flags=['--rollback-on-failure'])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertFalse(Path('created').exists())
        self.assertTrue(core.records(Path.cwd())[0]['undone'])

    def test_audit_write_reentry_is_not_blocked(self):
        config = Path('.agentguard/config.toml')
        config.write_text('[privacy]\nprotected_files = [".env", ".agentguard"]\n')
        result = self.execute("""from pathlib import Path
try: Path('.env').read_text()
except PermissionError: pass
else: raise AssertionError('not blocked')
""")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('file.open', self.runner.invoke(main, ['audit']).output)

    def test_requires_python_hooks_and_valid_configuration(self):
        result = self.runner.invoke(main, ['run', '--protect-files', '--', sys.executable, '-c', 'pass'])
        self.assertEqual(result.exit_code, 2)
        for invalid in ('".env"', '[1]', '[""]', '["../outside"]', '[]'):
            Path('.agentguard/config.toml').write_text('[privacy]\nprotected_files = ' + invalid)
            with patch('agentguard.cli.subprocess.run') as execute:
                result = self.runner.invoke(main, ['run', '--python-hooks', '--protect-files', '--', sys.executable, '-c', 'pass'])
            execute.assert_not_called()
            self.assertEqual(result.exit_code, 1, result.output)

    def test_relative_scope_and_environment_cleanup(self):
        root = Path.cwd()
        policy = FilePolicy(root, ['*.key'])
        self.assertIsNotNone(policy.match(root / 'nested/key.key'))
        self.assertIsNone(policy.match(root.parent / 'outside.key'))
        with patch.dict(os.environ, {'AGENTGUARD_PRIVACY': 'stale'}):
            self.assertNotIn('AGENTGUARD_PRIVACY', runtime.environment({}, False, False))
