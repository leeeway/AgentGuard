"""Read-only session inspection without exposing file contents by default."""
import base64
import difflib
import json

from . import core, objects, retention


def summary(record):
    complete = 'after' in record
    restored = record.get('restored_paths', [])
    remaining = [p for p in core.changes(record['before'], record['after']) if p not in restored] if complete else None
    state = 'incomplete' if not complete else 'partially-undone' if restored else 'complete'
    if record.get('undone'):
        state = 'undone'
    return {
        'id': record['id'],
        'profile': record.get('profile'),
        'pinned': record.get('pinned', False),
        'retention_reasons': retention.reasons(record),
        'state': state,
        'redo_paths': core.undone_paths(record),
        'redo_count': record.get('redo_count', 0),
        'restored_paths': record.get('restored_paths', []),
        'remaining_paths': (0 if record.get('undone') else len(remaining)) if complete else None,
        'started_at': record.get('started_at'),
        'finished_at': record.get('finished_at'),
        'recovered_at': record.get('recovered_at'),
        'exit_code': record.get('exit_code'),
        'command_exit_code': record.get('command_exit_code', record.get('exit_code')),
        'verification': record.get('verification', {'requested': 0, 'exit_codes': [], 'state': 'disabled'}),
        'verification_checkpoint': 'before_verification' in record,
        'hooks': record.get('hooks', []),
        'file_protection': record.get('file_protection', False),
        'snapshot_excludes': record.get('snapshot_excludes', []),
        'automatic_rollback': record.get('automatic_rollback', 'disabled'),
        'changed_paths': len(core.changes(record['before'], record['after'])) if complete else None,
    }


def select(history, session=None):
    if session is not None:
        matches = [record for record in history if record['id'] == session]
        if not matches:
            raise ValueError('Session not found: ' + session)
        return matches[0]
    if not history:
        raise ValueError('No recorded sessions.')
    return history[-1]


def diff(record, contents=False, paths=(), *, root=None, phase='session'):
    if phase != 'session' and 'before_verification' not in record:
        raise ValueError('No verification checkpoint available for this session.')
    if phase != 'agent' and 'after' not in record:
        raise ValueError('Session is incomplete; no final diff is available.')
    if phase == 'verification' and 'recovered_at' in record:
        raise ValueError('Verification diff is unavailable after recovery. Use the session diff.')
    start = 'before_verification' if phase == 'verification' else 'before'
    end = 'before_verification' if phase == 'agent' else 'after'
    before, after = record[start], record[end]
    changed = core.changes(before, after)
    for path in paths:
        if path not in changed:
            raise ValueError('Path has no recorded changes: ' + path)
    output = []
    for path in changed:
        if paths and path not in paths:
            continue
        old, new = before.get(path), after.get(path)
        label = 'added' if old is None else 'deleted' if new is None else 'modified'
        name = json.dumps(path, ensure_ascii=False)
        output.append(f'{label}: {name}\n')
        if not contents:
            continue
        for version, entry in [('before', old), ('after', new)]:
            if entry:
                details = entry['kind']
                if 'mode' in entry:
                    details += f" mode={entry['mode']:04o}"
                if entry['kind'] == 'link':
                    details += ' target=' + json.dumps(entry['target'], ensure_ascii=False)
                output.append(f'  {version}: {details}\n')
        if any(entry and entry['kind'] != 'file' for entry in (old, new)):
            continue
        data = []
        for entry in (old, new):
            if entry is None:
                data.append(b'')
            elif 'data' in entry:
                data.append(base64.b64decode(entry['data'], validate=True))
            else:
                data.append(objects.read(root, entry['object']))
        try:
            texts = [content.decode('utf-8') for content in data]
            if any('\x00' in text for text in texts):
                raise UnicodeError('binary')
        except UnicodeError:
            output.append('  Binary content omitted.\n')
            continue
        for line in difflib.unified_diff(
            texts[0].splitlines(keepends=True), texts[1].splitlines(keepends=True),
            fromfile='before/' + name, tofile='after/' + name,
        ):
            # Escape terminal control characters, while preserving tabs/newlines.
            line = ''.join(c if c in '\t\n' or c.isprintable() else ascii(c)[1:-1] for c in line)
            output.append(line)
            if not line.endswith('\n'):
                output.append('\n\\ No newline at end of file\n')
    return ''.join(output)
