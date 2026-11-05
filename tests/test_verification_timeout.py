from pathlib import Path
import shlex
import sys
import time
import unittest
from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core, processes


def command(code):
    return shlex.join([sys.executable, '-c', code])


class VerificationTimeout(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('source').write_text('before')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, validator, rollback=False):
        flags = ['--rollback-on-failure'] if rollback else []
        return self.runner.invoke(main, ['run', '--verify-timeout', '0.5', '--verify', command(validator)] + flags + ['--', sys.executable, '-c', "from pathlib import Path; Path('source').write_text('agent')"])

    def test_timeout_recovers_and_records_outcome(self):
        result = self.execute("import time; time.sleep(30)", rollback=True)
        self.assertEqual(result.exit_code, 124, result.output)
        self.assertEqual(Path('source').read_text(), 'before')
        record = core.records(Path.cwd())[-1]
        self.assertEqual(record['verification']['timed_out'], [1])
        self.assertEqual(record['verification']['exit_codes'], [124])
        self.assertEqual(record['verification']['timeout_seconds'], 0.5)
        self.assertTrue(record['undone'])
        self.assertFalse(Path('.agentguard/session.lock').exists())

    def test_timeout_keeps_changes_by_default(self):
        result = self.execute('import time; time.sleep(30)')
        self.assertEqual(result.exit_code, 124)
        self.assertEqual(Path('source').read_text(), 'agent')

    def test_quick_validation_and_stdin_eof(self):
        result = self.execute("import sys; assert sys.stdin.read() == ''")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(core.records(Path.cwd())[-1]['verification']['timed_out'], [])

    def test_invalid_timeout_does_not_launch(self):
        for duration in ('0', '-1', 'nan', 'inf'):
            result = self.runner.invoke(main, ['run', '--verify-timeout', duration, '--verify', 'echo ok', '--', 'echo', 'main'])
            self.assertEqual(result.exit_code, 2, result.output)
        result = self.runner.invoke(main, ['run', '--verify-timeout', '1', '--', 'echo', 'main'])
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(Path('.agentguard/snapshots').exists())

    def test_term_resistant_descendant_is_killed(self):
        # A descendant writes a marker much later unless group cleanup reaches it.
        child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1.8); Path('escaped').touch(); time.sleep(30)"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
        started = time.monotonic()
        code, timed_out = processes.verify([sys.executable, '-c', parent], None, timeout=0.5)
        self.assertEqual((code, timed_out), (124, True))
        time.sleep(max(0, 2.1 - (time.monotonic() - started)))
        self.assertFalse(Path('escaped').exists())
