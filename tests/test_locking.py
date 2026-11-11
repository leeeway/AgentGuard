import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest

from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core, locking


class WorkspaceLocks(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        self.folder = Path('.agentguard')
        self.env = os.environ.copy()
        self.env['PYTHONPATH'] = str(Path(core.__file__).resolve().parent.parent)

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def spawn(self, argv):
        return subprocess.Popen(argv, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    def ready(self, process):
        readable, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(readable, 'child did not announce readiness')
        line = process.stdout.readline().strip()
        self.assertTrue(line.isdecimal(), 'unexpected child output: ' + line)
        return int(line)

    def cleanup(self, process, child=None):
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def test_kernel_lock_prevents_overlap_even_if_marker_removed(self):
        with locking.workspace_lock(self.folder) as descriptor:
            self.assertFalse(os.get_inheritable(descriptor))
            self.assertEqual(locking.status(self.folder)['state'], 'active')
            (self.folder / 'session.lock').unlink()
            with self.assertRaisesRegex(ValueError, 'active'):
                with locking.workspace_lock(self.folder):
                    self.fail('overlapping workspace lock')
        guard_inode = (self.folder / 'workspace.lock').stat().st_ino
        with locking.workspace_lock(self.folder):
            pass
        self.assertEqual((self.folder / 'workspace.lock').stat().st_ino, guard_inode)
        self.assertEqual(locking.status(self.folder)['state'], 'idle')

    def test_exception_releases_lock_and_removes_owned_marker(self):
        with self.assertRaisesRegex(RuntimeError, 'test'):
            with locking.workspace_lock(self.folder):
                raise RuntimeError('test')
        self.assertFalse((self.folder / 'session.lock').exists())
        with locking.workspace_lock(self.folder):
            pass

    def test_sigkill_marker_is_diagnosed_and_reclaimed(self):
        source = "from pathlib import Path; import os,sys; from agentguard.locking import workspace_lock\nwith workspace_lock(Path('.agentguard')):\n print(os.getpid(), flush=True)\n sys.stdin.read()\n"
        process = self.spawn([sys.executable, '-c', source])
        try:
            self.ready(process)
            process.kill()
            process.wait(timeout=5)
            marker = self.folder / 'session.lock'
            contents = marker.read_bytes()
            guard_inode = (self.folder / 'workspace.lock').stat().st_ino
            report = self.runner.invoke(main, ['doctor', '--json'])
            self.assertEqual(report.exit_code, 3, report.output)
            check = next(c for c in json.loads(report.output)['checks'] if c['code'] == 'workspace-lock')
            self.assertEqual(check['state'], 'recoverable')
            self.assertEqual(marker.read_bytes(), contents)
            result = self.runner.invoke(main, ['gc', '--dry-run'])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertFalse(marker.exists())
            self.assertEqual((self.folder / 'workspace.lock').stat().st_ino, guard_inode)
        finally:
            self.cleanup(process)

    def test_live_direct_commands_retain_lock_after_wrapper_sigkill(self):
        python_child = [sys.executable, '-c', 'import os,time; print(os.getpid(),flush=True); time.sleep(30)']
        variants = [('agent-python', [], python_child),
                    ('verification', ['--verify', shlex.join(python_child)], [sys.executable, '-c', 'pass']),
                    ('bounded-verification', ['--verify-timeout', '30', '--verify', shlex.join(python_child)], [sys.executable, '-c', 'pass'])]
        if shutil.which('node'):
            variants.append(('agent-node', [], [shutil.which('node'), '-e', 'console.log(process.pid); setTimeout(()=>{},30000);']))
        for name, flags, command in variants:
            with self.subTest(name=name):
                process = self.spawn([sys.executable, '-m', 'agentguard.cli', 'run'] + flags + ['--'] + command)
                child = None
                try:
                    child = self.ready(process)
                    process.kill()
                    process.wait(timeout=5)
                    self.assertEqual(locking.status(self.folder)['state'], 'active')
                    for invocation in (['run', '--', sys.executable, '-c', 'pass'], ['undo'], ['gc']):
                        result = self.runner.invoke(main, invocation)
                        self.assertEqual(result.exit_code, 1, result.output)
                        self.assertIn('active', result.output)
                    report = json.loads(self.runner.invoke(main, ['doctor', '--json']).output)
                    self.assertEqual(next(c['state'] for c in report['checks'] if c['code'] == 'workspace-lock'), 'active')
                finally:
                    self.cleanup(process, child)
                # Wait for the killed direct process to close its inherited fd.
                deadline = time.monotonic() + 3
                while locking.status(self.folder)['state'] == 'active' and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(locking.status(self.folder)['state'], 'recoverable')
                result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(locking.status(self.folder)['state'], 'idle')

    def test_legacy_and_corrupt_markers_are_not_reclaimed(self):
        for contents in ('', '{', '{"version":2,"pid":1,"token":"bad"}'):
            marker = self.folder / 'session.lock'
            marker.write_text(contents)
            with self.assertRaisesRegex(ValueError, 'legacy'):
                with locking.workspace_lock(self.folder):
                    self.fail('legacy marker overwritten')
            self.assertEqual(marker.read_text(), contents)
            self.assertEqual(locking.status(self.folder)['state'], 'legacy')

    def test_symlink_lock_paths_refused_without_touching_targets(self):
        Path('outside').write_text('keep')
        for name in ('workspace.lock', 'session.lock'):
            path = self.folder / name
            if path.exists():
                path.unlink()
            path.symlink_to(Path('outside').resolve())
            result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(Path('outside').read_text(), 'keep')
            path.unlink()
