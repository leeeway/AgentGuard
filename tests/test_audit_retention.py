import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard import audit, core
from agentguard.cli import main
from agentguard.locking import workspace_lock


class AuditRetention(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def log(self, index, snapshot=False, **fields):
        session = f'{index:020d}-' + f'{index:032x}'
        path = audit.initialize(Path.cwd(), session)
        audit.append(path, 'cli', 'launch', 'allowed', [])
        if snapshot:
            core.save(Path.cwd(), {'id': session, 'before': {}, 'after': {}, **fields})
        return session, path

    def plan(self, *args):
        result = self.runner.invoke(main, ['audit-prune', '--json', '--dry-run'] + list(args))
        self.assertEqual(result.exit_code, 0, result.output)
        return json.loads(result.output)

    def test_preview_counts_bytes_and_never_reads_log_contents(self):
        old = self.log(1)
        new = self.log(2)
        linked = self.log(3, snapshot=True)
        before = {p.name: p.read_bytes() for p in Path('.agentguard/audit').iterdir()}
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('log payload read')):
            report = self.plan('--keep', '1')
        self.assertEqual([c['session'] for c in report['candidates']], [old[0]])
        self.assertEqual(report['candidate_bytes'], len(before[old[1].name]))
        self.assertEqual([c['session'] for c in report['retained_archived']], [new[0]])
        self.assertEqual([c['session'] for c in report['snapshot_linked']], [linked[0]])
        self.assertEqual(report['removed_count'], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path('.agentguard/audit').iterdir()})

    def test_cleanup_protects_all_retained_snapshot_logs(self):
        archived = self.log(1)
        logs = [self.log(2, snapshot=True), self.log(3, snapshot=True, pinned=True),
                self.log(4, snapshot=True, automatic_rollback='failed')]
        incomplete_id, incomplete_path = self.log(5)
        core.save(Path.cwd(), {'id': incomplete_id, 'before': {}})
        result = self.runner.invoke(main, ['audit-prune', '--keep', '0'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(archived[1].exists())
        self.assertTrue(all(path.exists() for _, path in logs))
        self.assertTrue(incomplete_path.exists())
        self.assertEqual(len(core.records(Path.cwd())), 4)

    def test_default_keeps_30_archived_logs_and_ignores_unknown_files(self):
        logs = [self.log(i) for i in range(1, 33)]
        extra = Path('.agentguard/audit/notes.jsonl')
        extra.write_text('PRIVATE_NOTES')
        report = self.plan()
        self.assertEqual(len(report['candidates']), 2)
        self.assertEqual(len(report['retained_archived']), 30)
        self.assertEqual(report['ignored_entries'], 1)
        self.assertNotIn('PRIVATE_NOTES', json.dumps(report))
        self.assertEqual(self.runner.invoke(main, ['audit-prune']).exit_code, 0)
        self.assertFalse(logs[0][1].exists())
        self.assertTrue(extra.exists())

    def test_auto_retention_is_opt_in_and_follows_snapshot_pruning(self):
        old_id, old_path = self.log(1)
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(old_path.exists())
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('max_history = 30', 'max_history = 1') + '\n[audit]\nmax_archived_sessions = 0\n')
        previous = core.records(Path.cwd())[-1]['id']
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(old_path.exists())
        self.assertFalse(audit.path_for(Path.cwd(), previous).exists())
        latest = core.records(Path.cwd())[-1]['id']
        self.assertTrue(audit.path_for(Path.cwd(), latest).exists())
        self.assertEqual(self.plan()['keep_archived'], 0)
        self.assertEqual(self.plan('--keep', '3')['keep_archived'], 3)

    def test_snapshot_corruption_aborts_before_deletion(self):
        _, archived = self.log(1)
        session, _ = self.log(2, snapshot=True)
        manifest = Path('.agentguard/snapshots') / (session + '.json')
        manifest.write_text('{')
        result = self.runner.invoke(main, ['audit-prune', '--keep', '0'])
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(archived.exists())

    def test_symlinks_and_directories_abort_before_deletion(self):
        _, archived = self.log(1)
        unsafe = audit.path_for(Path.cwd(), f'{2:020d}-' + f'{2:032x}')
        Path('outside').write_text('keep')
        for kind in ('link', 'directory'):
            if kind == 'link':
                unsafe.symlink_to(Path('outside').resolve())
            else:
                unsafe.mkdir()
            result = self.runner.invoke(main, ['audit-prune', '--keep', '0'])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertTrue(archived.exists())
            self.assertEqual(Path('outside').read_text(), 'keep')
            if kind == 'link':
                unsafe.unlink()
            else:
                unsafe.rmdir()

    def test_workspace_lock_is_respected(self):
        _, path = self.log(1)
        with workspace_lock(Path('.agentguard')):
            for flags in ([], ['--dry-run']):
                result = self.runner.invoke(main, ['audit-prune', '--keep', '0'] + flags)
                self.assertEqual(result.exit_code, 1)
                self.assertIn('active', result.output)
        self.assertTrue(path.exists())

    def test_invalid_config_and_empty_storage(self):
        self.assertEqual(self.plan()['candidates'], [])
        self.assertFalse(Path('.agentguard/audit').exists())
        for text in ('audit = []', '[audit]\nmax_archived_sessions = -1',
                     '[audit]\nmax_archived_sessions = true', '[audit]\nmax_archived_sessions = "3"'):
            Path('.agentguard/config.toml').write_text(text)
            result = self.runner.invoke(main, ['audit-prune'])
            self.assertEqual(result.exit_code, 1, result.output)
