import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard import core, objects
from agentguard.cli import main
from agentguard.locking import workspace_lock


class RedoSessions(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('a').write_text('before a')
        Path('b').write_text('before b')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def run_script(self, source="from pathlib import Path; Path('a').write_text('agent a'); Path('b').write_text('agent b')"):
        result = self.runner.invoke(main, ['run', '--', sys.executable, '-c', source])
        self.assertEqual(result.exit_code, 0, result.output)
        return core.records(Path.cwd())[-1]

    def invoke(self, *args, code=0):
        result = self.runner.invoke(main, list(args))
        self.assertEqual(result.exit_code, code, result.output)
        return result

    def test_roundtrip_restores_binary_modes_links_and_directory_types(self):
        Path('dir').mkdir()
        Path('dir/child').write_bytes(b'before')
        Path('link').symlink_to('a')
        source = "from pathlib import Path; Path('a').write_bytes(b'\\x00after'); Path('a').chmod(0o755); Path('b').unlink(); Path('b').mkdir(); Path('b/new').touch(); Path('dir/child').unlink(); Path('dir').rmdir(); Path('dir').write_text('file'); Path('link').unlink(); Path('link').symlink_to('dir')"
        record = self.run_script(source)
        log = Path('.agentguard/audit') / (record['id'] + '.jsonl')
        audit_before = log.read_bytes()
        self.invoke('undo')
        self.assertEqual(core.capture(Path.cwd()), record['before'])
        with patch('agentguard.cli.subprocess.run') as run:
            self.invoke('redo')
        run.assert_not_called()
        self.assertEqual(core.capture(Path.cwd()), record['after'])
        self.assertEqual(log.read_bytes(), audit_before)
        self.assertFalse(core.records(Path.cwd())[-1]['undone'])
        self.invoke('undo')
        self.assertEqual(core.capture(Path.cwd()), record['before'])

    def test_partial_undo_redo_uses_mixed_directory_state(self):
        Path('d').mkdir()
        Path('d/file').write_text('before')
        self.run_script("from pathlib import Path; Path('d').chmod(0o750); Path('d/file').write_text('agent'); Path('new').mkdir(); Path('new/file').touch(); Path('a').write_text('agent a')")
        self.invoke('undo', '--path', 'd/file', '--path', 'new/file')
        Path('a').write_text('later human edit')
        self.invoke('redo', '--path', 'd/file', '--path', 'new/file')
        self.assertEqual(Path('d/file').read_text(), 'agent')
        self.assertTrue(Path('new/file').exists())
        self.assertEqual(Path('a').read_text(), 'later human edit')
        self.assertEqual(Path('d').stat().st_mode & 0o777, 0o750)
        row = json.loads(self.invoke('history', '--json').output)[0]
        self.assertEqual(row['state'], 'complete')
        self.assertEqual(row['redo_paths'], [])
        self.assertEqual(row['redo_count'], 1)

    def test_selective_redo_then_remaining_redo_and_undo(self):
        self.run_script()
        self.invoke('undo')
        self.invoke('redo', '--path', 'a')
        self.assertEqual(Path('a').read_text(), 'agent a')
        self.assertEqual(Path('b').read_text(), 'before b')
        row = json.loads(self.invoke('history', '--json').output)[0]
        self.assertEqual(row['state'], 'partially-undone')
        self.assertEqual(row['redo_paths'], ['b'])
        self.assertIn('partially-restored', row['retention_reasons'])
        self.invoke('redo')
        self.assertEqual(Path('b').read_text(), 'agent b')
        self.assertEqual(core.records(Path.cwd())[-1]['redo_count'], 2)
        self.invoke('undo')
        self.assertEqual(Path('a').read_text(), 'before a')
        self.assertEqual(Path('b').read_text(), 'before b')

    def test_conflicts_refuse_entire_selection_and_preserve_metadata(self):
        self.run_script()
        self.invoke('undo')
        record = core.records(Path.cwd())[-1]
        Path('a').write_text('human edit')
        self.invoke('redo', code=1)
        self.assertEqual(Path('a').read_text(), 'human edit')
        self.assertEqual(Path('b').read_text(), 'before b')
        self.assertEqual(core.records(Path.cwd())[-1], record)
        self.invoke('redo', '--path', 'b')
        self.assertEqual(Path('a').read_text(), 'human edit')
        self.assertEqual(Path('b').read_text(), 'agent b')

    def test_preview_is_read_only_and_invalid_path_or_directory_refused(self):
        self.run_script("from pathlib import Path; Path('a').write_text('agent'); Path('d').mkdir(); Path('d/file').touch()")
        self.invoke('undo')
        record = core.records(Path.cwd())[-1]
        workspace = core.capture(Path.cwd())
        self.assertIn('would reapply: "a"', self.invoke('redo', '--dry-run').output)
        self.assertEqual(core.capture(Path.cwd()), workspace)
        self.assertEqual(core.records(Path.cwd())[-1], record)
        for path in ('../escape', 'missing', 'd', 'd/file'):
            self.invoke('redo', '--path', path, code=1)
        self.assertEqual(core.capture(Path.cwd()), workspace)

    def test_excluded_content_blocks_directory_removal_before_mutation(self):
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('[snapshots]', '[snapshots]\nexclude = ["d/cache"]'))
        Path('d').mkdir()
        Path('d/cache').write_text('excluded')
        self.run_script("from pathlib import Path; Path('d/cache').unlink(); Path('d').rmdir(); Path('a').write_text('agent')")
        self.invoke('undo')
        Path('d/cache').write_text('human cache')
        self.assertIn('d/cache', self.invoke('redo', code=1).output)
        self.assertEqual(Path('d/cache').read_text(), 'human cache')
        self.assertEqual(Path('a').read_text(), 'before a')

    def test_legacy_undone_record_and_pinned_metadata(self):
        record = {'id': '0' * 20 + '-' + 'a' * 32,
                  'before': {'a': {'kind': 'file', 'mode': 0o644, 'data': base64.b64encode(b'before a').decode()}},
                  'after': {'a': {'kind': 'file', 'mode': 0o755, 'data': base64.b64encode(b'legacy agent').decode()}},
                  'undone': True, 'pinned': True}
        core.save(Path.cwd(), record)
        self.invoke('redo', '--session', record['id'])
        self.assertEqual(Path('a').read_text(), 'legacy agent')
        self.assertTrue(core.records(Path.cwd())[0]['pinned'])

    def test_corrupt_objects_prevent_redo_and_lock_is_respected(self):
        self.run_script()
        self.invoke('undo')
        with workspace_lock(Path('.agentguard')):
            self.assertIn('active', self.invoke('redo', code=1).output)
        path = Path('.agentguard/objects') / objects.digest(b'agent b')
        path.write_bytes(b'corrupted')
        self.assertIn('checksum mismatch', self.invoke('redo', code=1).output)
        self.assertEqual(Path('a').read_text(), 'before a')
        self.assertEqual(Path('b').read_text(), 'before b')

    def test_repeated_undos_redo_in_dependency_order(self):
        self.run_script("from pathlib import Path; Path('a').write_text('first')")
        self.run_script("from pathlib import Path; Path('a').write_text('second')")
        self.invoke('undo')
        self.invoke('undo')
        self.assertEqual(Path('a').read_text(), 'before a')
        self.invoke('redo')
        self.assertEqual(Path('a').read_text(), 'first')
        self.invoke('redo')
        self.assertEqual(Path('a').read_text(), 'second')

    def test_undo_after_selective_redo_under_already_restored_directory(self):
        Path('d').mkdir()
        Path('d/file').write_text('before')
        self.run_script("from pathlib import Path; Path('d/file').unlink(); Path('d').rmdir()")
        self.invoke('undo')
        self.invoke('redo', '--path', 'd/file')
        self.assertTrue(Path('d').is_dir())
        self.assertFalse(Path('d/file').exists())
        self.invoke('undo')
        self.assertEqual(Path('d/file').read_text(), 'before')
        self.invoke('redo')
        self.assertFalse(Path('d').exists())

    def test_redo_can_recover_automatically_rolled_back_results(self):
        source = "from pathlib import Path; Path('a').write_text('failed result'); raise SystemExit(7)"
        self.invoke('run', '--rollback-on-failure', '--', sys.executable, '-c', source, code=7)
        self.assertEqual(Path('a').read_text(), 'before a')
        self.invoke('redo')
        self.assertEqual(Path('a').read_text(), 'failed result')
        record = core.records(Path.cwd())[-1]
        self.assertEqual(record['exit_code'], 7)
        self.assertEqual(record['automatic_rollback'], 'restored')
        self.assertFalse(record['undone'])

    def test_no_redo_and_explicit_session_errors(self):
        self.assertIn('No sessions to redo', self.invoke('redo').output)
        record = self.run_script()
        self.invoke('redo', '--session', record['id'], code=1)
        self.invoke('redo', '--session', 'missing', code=1)
        incomplete = {'id': '0' * 20 + '-' + 'b' * 32, 'before': {}}
        core.save(Path.cwd(), incomplete)
        self.assertIn('incomplete', self.invoke('redo', '--session', incomplete['id'], code=1).output)
