import json
from pathlib import Path
import shutil
import sys
import unittest

from click.testing import CliRunner
from agentguard import audit, core
from agentguard.cli import main


class AuditEvents(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('.agentguard/config.toml').write_text('[safety]\ndangerous_commands = "block"\nblocked_patterns = ["FORBIDDEN"]')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def rows(self, *args):
        result = self.runner.invoke(main, ['audit', '--json'] + list(args))
        self.assertEqual(result.exit_code, 0, result.output)
        return json.loads(result.output)

    def test_python_allowed_blocked_and_no_argument_disclosure(self):
        Path('worker.py').write_text("""import os, subprocess, sys
subprocess.run([sys.executable, '-c', 'pass'])
try: os.system('echo FORBIDDEN_PRIVATE_TOKEN')
except PermissionError: pass
""")
        result = self.runner.invoke(main, ['run', '--python-hooks', '--', sys.executable, 'worker.py'])
        self.assertEqual(result.exit_code, 0, result.output)
        rows = self.rows()
        self.assertEqual(rows[0]['source'], 'cli')
        self.assertTrue(any(r['source'] == 'python' and r['decision'] == 'allowed' for r in rows))
        self.assertTrue(any(r['source'] == 'python' and r['decision'] == 'blocked' and r['rules'] == [1] for r in rows))
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(rows))
        self.assertEqual(len(self.rows('--limit', '1')), 1)
        session = core.records(Path.cwd())[0]['id']
        self.assertEqual(rows, self.rows('--session', session))
        self.assertEqual(audit.path_for(Path.cwd(), session).stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
    def test_node_decisions(self):
        Path('worker.cjs').write_text("""const cp = require('node:child_process');
cp.execFileSync(process.execPath, ['-e', '']);
try { cp.execSync('echo FORBIDDEN_PRIVATE_TOKEN'); } catch(e) { if(e.code !== 'AGENTGUARD_BLOCKED') throw e; }
""")
        result = self.runner.invoke(main, ['run', '--node-hooks', '--', shutil.which('node'), 'worker.cjs'])
        self.assertEqual(result.exit_code, 0, result.output)
        rows = self.rows()
        self.assertTrue(any(r['source'] == 'node' and r['decision'] == 'blocked' for r in rows))
        self.assertTrue(any(r['source'] == 'node' and r['decision'] == 'allowed' for r in rows))
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(rows))

    def test_legacy_session_has_no_events(self):
        core.save(Path.cwd(), {'id': '0' * 20 + '-' + 'a' * 32, 'before': {}, 'after': {}})
        self.assertEqual(self.rows(), [])

    def test_corrupt_log_and_symlink_rejected(self):
        self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        path = audit.path_for(Path.cwd(), core.records(Path.cwd())[0]['id'])
        path.write_text('{')
        result = self.runner.invoke(main, ['audit'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('line 1', result.output)
        path.unlink()
        Path('outside').write_text('secret')
        path.symlink_to(Path('outside').resolve())
        self.assertNotEqual(self.runner.invoke(main, ['audit']).exit_code, 0)
        self.assertEqual(Path('outside').read_text(), 'secret')

    def test_verification_decision_and_recovery_preserve_audit(self):
        import shlex
        check = shlex.join([sys.executable, '-c', 'raise SystemExit(5)'])
        result = self.runner.invoke(main, ['run', '--rollback-on-failure', '--verify', check, '--', sys.executable, '-c', 'pass'])
        self.assertEqual(result.exit_code, 5)
        self.assertEqual([r['event'] for r in self.rows()], ['launch', 'verification.1'])

    def test_invalid_storage_prevents_launch(self):
        Path('outside').mkdir()
        Path('.agentguard/audit').symlink_to(Path('outside').resolve(), target_is_directory=True)
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', "from pathlib import Path; Path('marker').touch()"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(Path('marker').exists())
        self.assertEqual(list(Path('outside').iterdir()), [])
