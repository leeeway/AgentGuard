"""Validate snapshot structure completely before changing workspace files."""
import base64
import binascii
from pathlib import PurePosixPath
import re
import math


def manifest(value):
    if not isinstance(value, dict):
        raise ValueError('Snapshot manifest must be an object')
    for path, entry in value.items():
        if not isinstance(path, str) or not path or '\x00' in path or any(
            part in ('', '.', '..', '.git', '.agentguard') for part in path.split('/')
        ):
            raise ValueError('Unsafe path in snapshot')
        if not isinstance(entry, dict) or entry.get('kind') not in ('file', 'dir', 'link'):
            raise ValueError('Invalid snapshot entry kind')
        if entry['kind'] == 'link':
            if not isinstance(entry.get('target'), str) or not entry['target'] or '\x00' in entry['target']:
                raise ValueError('Invalid snapshot link target')
        else:
            mode = entry.get('mode')
            if type(mode) is not int or not 0 <= mode <= 0o7777:
                raise ValueError('Invalid snapshot file mode')
        if entry['kind'] == 'file':
            try:
                if not isinstance(entry.get('data'), str):
                    raise ValueError('Missing file content')
                base64.b64decode(entry['data'], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError('Invalid snapshot file content') from exc
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.':
                ancestor = value.get(str(parent))
                if not isinstance(ancestor, dict) or ancestor.get('kind') != 'dir':
                    raise ValueError('Snapshot parent must be a recorded directory')


def record(value):
    if not isinstance(value, dict):
        raise ValueError('Snapshot record must be an object')
    manifest(value.get('before'))
    if 'after' in value:
        manifest(value['after'])
    if not isinstance(value.get('id'), str) or not re.fullmatch(r'[0-9]{20}-[0-9a-f]{32}', value['id']):
        raise ValueError('Invalid snapshot identifier')
    if 'undone' in value and type(value['undone']) is not bool:
        raise ValueError('Invalid snapshot undo state')
    if 'exit_code' in value and type(value['exit_code']) is not int:
        raise ValueError('Invalid snapshot exit code')
    if 'command_exit_code' in value and type(value['command_exit_code']) is not int:
        raise ValueError('Invalid command exit code')
    if 'verification' in value:
        check = value['verification']
        if (not isinstance(check, dict) or type(check.get('requested')) is not int
                or check['requested'] < 0 or not isinstance(check.get('exit_codes'), list)
                or len(check['exit_codes']) > check['requested']
                or any(type(code) is not int for code in check['exit_codes'])
                or check.get('state') not in ('disabled', 'pending', 'skipped', 'passed', 'failed')):
            raise ValueError('Invalid verification metadata')
        timeout = check.get('timeout_seconds')
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('Invalid verification timeout')
        timed_out = check.get('timed_out', [])
        if (not isinstance(timed_out, list) or any(type(i) is not int or i < 1
                or i > len(check['exit_codes']) or check['exit_codes'][i - 1] != 124 for i in timed_out)):
            raise ValueError('Invalid verification timeout results')
    if 'hooks' in value and (not isinstance(value['hooks'], list) or any(h not in ('python', 'node') for h in value['hooks'])):
        raise ValueError('Invalid snapshot hooks')
    if value.get('automatic_rollback', 'disabled') not in ('disabled', 'pending', 'not-needed', 'restored', 'failed', 'not-started'):
        raise ValueError('Invalid automatic rollback state')
    for field in ('started_at', 'finished_at'):
        if field in value and not isinstance(value[field], str):
            raise ValueError('Invalid snapshot timestamp')
