import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard import audit, core, objects, retention
from agentguard.cli import main
from agentguard.locking import workspace_lock


class SessionRetention(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        path = Path('.agentguard/config.toml')
        path.write_text(path.read_text().replace('max_history = 30', 'max_history = 1'))

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def record(self, index, **fields):
        data = base64.b64encode(f'content{index}'.encode()).decode()
        value = {'id': f'{index:020d}-' + f'{index:032x}', 'before': {},
                 'after': {'file': {'kind': 'file', 'mode': 0o644, 'data': data}}}
        value.update(fields)
        core.save(Path.cwd(), value)
        return value

    def ids(self):
        return [record['id'] for record in core.records(Path.cwd())]

    def test_pin_keeps_content_through_prune_and_gc_then_unpin_releases_it(self):
        first, second, third = (self.record(i) for i in (1, 2, 3))
        log = audit.initialize(Path.cwd(), first['id'])
        audit.append(log, 'cli', 'launch', 'allowed', [])
        self.assertEqual(self.runner.invoke(main, ['pin', '--session', first['id']]).exit_code, 0)
        result = self.runner.invoke(main, ['prune'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.ids(), [first['id'], third['id']])
        self.assertEqual(self.runner.invoke(main, ['gc']).exit_code, 0)
        self.assertEqual(objects.read(Path.cwd(), objects.digest(b'content1')), b'content1')
        self.assertFalse((Path('.agentguard/objects') / objects.digest(b'content2')).exists())
        self.assertEqual(self.runner.invoke(main, ['unpin', '--session', first['id']]).exit_code, 0)
        self.assertEqual(self.runner.invoke(main, ['prune']).exit_code, 0)
        self.assertEqual(self.ids(), [third['id']])
        self.assertEqual(self.runner.invoke(main, ['gc']).exit_code, 0)
        self.assertFalse((Path('.agentguard/objects') / objects.digest(b'content1')).exists())
        self.assertEqual(len(json.loads(self.runner.invoke(main, ['audit', '--session', first['id'], '--json']).output)), 1)

    def test_preview_protects_unfinished_and_partially_restored_sessions(self):
        incomplete = self.record(1)
        incomplete.pop('after')
        core.save(Path.cwd(), incomplete)
        partial = self.record(2)
        partial['after']['other'] = partial['after']['file'].copy()
        partial['restored_paths'] = ['file']
        core.save(Path.cwd(), partial)
        failed = self.record(3, automatic_rollback='failed')
        pending = self.record(4, automatic_rollback='pending')
        pinned = self.record(5, pinned=True)
        eligible = self.record(6, automatic_rollback='failed', undone=True)
        latest = self.record(7)
        folder = Path('.agentguard/snapshots')
        original = {p.name: p.read_bytes() for p in folder.iterdir()}
        result = self.runner.invoke(main, ['prune', '--dry-run', '--json'])
        self.assertEqual(result.exit_code, 0, result.output)
        plan = json.loads(result.output)
        self.assertEqual(plan['candidates'], [eligible['id']])
        self.assertEqual(plan['kept_regular'], [latest['id']])
        self.assertEqual([p['reasons'] for p in plan['protected']], [['incomplete'], ['partially-restored'], ['unfinished-recovery'], ['unfinished-recovery'], ['pinned']])
        self.assertEqual(plan['removed_count'], 0)
        self.assertEqual(original, {p.name: p.read_bytes() for p in folder.iterdir()})
        self.assertEqual(self.runner.invoke(main, ['prune']).exit_code, 0)
        self.assertNotIn(eligible['id'], self.ids())
        self.assertTrue(all(r['id'] in self.ids() for r in (incomplete, partial, failed, pending, pinned)))

    def test_automatic_history_pruning_uses_same_policy(self):
        pinned = self.record(1, pinned=True)
        incomplete = {'id': f'{2:020d}-' + f'{2:032x}', 'before': {}}
        core.save(Path.cwd(), incomplete)
        for _ in range(2):
            result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', 'pass'])
            self.assertEqual(result.exit_code, 0, result.output)
        retained = core.records(Path.cwd())
        self.assertEqual(len(retained), 3)
        self.assertTrue(all(value['id'] in self.ids() for value in (pinned, incomplete)))
        rows = json.loads(self.runner.invoke(main, ['history', '--json']).output)
        pin = next(row for row in rows if row['id'] == pinned['id'])
        self.assertTrue(pin['pinned'])
        self.assertEqual(pin['retention_reasons'], ['pinned'])

    def test_pin_latest_and_unknown_id_and_idempotence(self):
        first, second = self.record(1), self.record(2)
        self.assertEqual(self.runner.invoke(main, ['pin']).exit_code, 0)
        self.assertFalse(core.records(Path.cwd())[0].get('pinned', False))
        self.assertTrue(core.records(Path.cwd())[1]['pinned'])
        with patch('agentguard.cli.core.save') as save:
            self.assertEqual(self.runner.invoke(main, ['pin']).exit_code, 0)
        save.assert_not_called()
        result = self.runner.invoke(main, ['pin', '--session', 'missing'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Session not found', result.output)
        self.assertEqual(self.runner.invoke(main, ['unpin']).exit_code, 0)
        self.assertFalse(core.records(Path.cwd())[1]['pinned'])
        self.assertEqual(self.ids(), [first['id'], second['id']])

    def test_corruption_stops_pruning_before_any_deletion(self):
        for index in (1, 2, 3):
            self.record(index)
        damaged = Path('.agentguard/objects') / objects.digest(b'content3')
        damaged.write_bytes(b'corrupt')
        paths = set(Path('.agentguard/snapshots').iterdir())
        result = self.runner.invoke(main, ['prune'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('checksum mismatch', result.output)
        self.assertEqual(set(Path('.agentguard/snapshots').iterdir()), paths)

    def test_mutations_and_preview_respect_live_workspace_lock(self):
        self.record(1)
        with workspace_lock(Path('.agentguard')):
            for command in (['pin'], ['unpin'], ['prune'], ['prune', '--dry-run']):
                result = self.runner.invoke(main, command)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn('active', result.output)
        self.assertFalse(core.records(Path.cwd())[0].get('pinned', False))

    def test_pin_type_validated_and_empty_history_handled(self):
        with self.assertRaisesRegex(ValueError, 'pin state'):
            self.record(1, pinned='yes')
        result = self.runner.invoke(main, ['pin'])
        self.assertEqual(result.exit_code, 1)
        plan = json.loads(self.runner.invoke(main, ['prune', '--dry-run', '--json']).output)
        self.assertEqual(plan['candidates'], [])
        self.assertEqual(plan['protected'], [])
