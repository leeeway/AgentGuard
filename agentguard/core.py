"""Local session snapshots. This module is not an operating-system sandbox."""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid

from . import validation, objects, inventory, privacy, profiles

try:
    import tomllib
except ImportError:
    import tomli as tomllib

DEFAULT_CONFIG = '''[safety]
dangerous_commands = "block"
blocked_patterns = ['\\brm\\b.*(?:-[a-zA-Z]*[rf]|--recursive|--force)', '\\bgit\\s+push\\b.*(?:--force|-f)', '\\bgit\\s+reset\\s+--hard', '\\bmkfs', '\\bdd\\s+if=', '>\\s*/etc/']

[privacy]
protected_files = ['.env*', '**/.env*', '*.pem', '*.key', '~/.ssh', '~/.aws/credentials', '~/.config/gcloud']

[snapshots]
max_history = 30
'''


def store(root):
    path = root / '.agentguard'
    if path.is_symlink():
        raise ValueError('.agentguard must not be a symbolic link')
    return path


def initialize(root):
    directory = store(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    config = directory / 'config.toml'
    if not config.exists():
        config.write_text(DEFAULT_CONFIG)


def config(root):
    path = store(root) / 'config.toml'
    if not path.is_file():
        raise ValueError('Run agentguard init in this directory first.')
    with path.open('rb') as stream:
        data = tomllib.load(stream)
    for section in ('safety', 'snapshots', 'privacy'):
        if not isinstance(data.get(section, {}), dict):
            raise ValueError(f'{section} must be a TOML table')
    safety = data.get('safety', {})
    if safety.get('dangerous_commands', 'block') not in ('block', 'ask', 'warn'):
        raise ValueError('Invalid dangerous_commands mode')
    patterns = safety.get('blocked_patterns', [])
    if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
        raise ValueError('safety.blocked_patterns must be an array of strings')
    for index, pattern in enumerate(patterns, 1):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'Invalid safety rule {index}: {exc.msg}') from exc
    maximum = data.get('snapshots', {}).get('max_history', 30)
    if type(maximum) is not int or maximum < 1:
        raise ValueError('max_history must be a positive integer')
    for key in ('max_file_bytes', 'max_total_bytes'):
        limit = data.get('snapshots', {}).get(key)
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError(f'snapshots.{key} must be a positive integer')
    validation.exclusions(data.get('snapshots', {}).get('exclude', []))
    privacy.validate(data.get('privacy', {}).get('protected_files', privacy.DEFAULT_PROTECTED))
    profiles.validate(data.get('profiles', {}))
    return data


def evaluate(command, settings):
    """Explain launch checks without executing the command or exposing arguments."""
    safety = settings.get('safety', {})
    mode = safety.get('dangerous_commands', 'block')
    patterns = safety.get('blocked_patterns', [])
    rendered = shlex.join(command) + '\n' + ' '.join(command)
    matched = [index for index, pattern in enumerate(patterns, 1) if re.search(pattern, rendered)]
    decision = {'block': 'blocked', 'ask': 'confirmation-required', 'warn': 'warning'}[mode] if matched else 'allowed'
    return {'decision': decision, 'mode': mode, 'matched_rules': matched,
            'rule_count': len(patterns), 'scope': 'launch-command'}


def dangerous(command, settings):
    return bool(evaluate(command, settings)['matched_rules'])


def capture(root, settings=None):
    limits = (settings or {}).get('snapshots', {})
    result = {}
    total = 0
    for path, info in inventory.entries(root, limits.get('exclude', [])):
        key = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            result[key] = {'kind': 'link', 'target': os.readlink(path)}
        elif stat.S_ISDIR(info.st_mode):
            result[key] = {'kind': 'dir', 'mode': mode}
        elif stat.S_ISREG(info.st_mode):
            available = []
            if 'max_file_bytes' in limits:
                available.append(limits['max_file_bytes'])
            if 'max_total_bytes' in limits:
                available.append(limits['max_total_bytes'] - total)
            cap = min(available) if available else None
            if cap is not None and info.st_size > cap:
                raise ValueError(f'Snapshot byte budget exceeded at {json.dumps(key)}')
            # Bound the read too, in case a file grows after lstat.
            with open(path, 'rb', opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW)) as stream:
                content = stream.read(cap + 1) if cap is not None else stream.read()
            if cap is not None and len(content) > cap:
                raise ValueError(f'Snapshot byte budget exceeded at {json.dumps(key)}')
            total += len(content)
            result[key] = {'kind': 'file', 'mode': mode, 'data': base64.b64encode(content).decode('ascii')}
        else:
            raise ValueError(f'Unsupported special file: {json.dumps(key)}')
    return result


def snapshot_directory(root):
    directory = store(root) / 'snapshots'
    if directory.is_symlink():
        raise ValueError('Snapshot directory must not be a symbolic link')
    if directory.exists() and not directory.is_dir():
        raise ValueError('Snapshot storage must be a directory')
    return directory


def save(root, record):
    validation.record(record)
    directory = snapshot_directory(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    if not re.fullmatch(r'[0-9]{20}-[0-9a-f]{32}', record['id']):
        raise ValueError('Invalid snapshot identifier')
    packed = objects.pack(root, record)
    path = directory / (record['id'] + '.json')
    temporary = directory / (uuid.uuid4().hex + '.tmp')
    try:
        with open(temporary, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump(packed, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_record(root, path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Snapshot record must be a regular file')
    try:
        record = objects.unpack(root, json.loads(path.read_text()))
        validation.record(record)
        if path.stem != record['id']:
            raise ValueError('Snapshot filename does not match its identifier')
    except ValueError as exc:
        raise ValueError(f'Invalid snapshot {path.name}: {exc}') from exc
    return record


def records(root):
    return [load_record(root, path) for path in sorted(snapshot_directory(root).glob('*.json'))]


def changes(before, after):
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def restore(root, record, dry_run=False, paths=()):
    validation.record(record)
    if 'after' not in record:
        raise ValueError('Session is incomplete; automatic undo cannot safely check for later edits.')
    before, after = record['before'], record['after']
    current = capture(root, {'snapshots': {'exclude': record.get('snapshot_excludes', [])}})
    original_changes = changes(before, after)
    restored = set(record.get('restored_paths', []))
    remaining = [key for key in original_changes if key not in restored]
    touched = sorted(set(paths)) if paths else remaining
    for key in touched:
        if key not in remaining:
            raise ValueError('Path is unchanged or already restored: ' + json.dumps(key))
        if paths:
            if any(entry.get(key, {}).get('kind') == 'dir' for entry in (before, after)):
                raise ValueError('Selective undo supports files and links; use full undo for directory changes.')
            for parent in Path(key).parents:
                if str(parent) != '.' and current.get(parent.as_posix(), {}).get('kind') != 'dir':
                    raise ValueError('Selective undo requires existing parent directories; use full undo.')
    conflicts = [k for k in touched if current.get(k) != after.get(k)]
    for key in touched:
        for parent in Path(key).parents:
            if str(parent) == '.':
                continue
            ancestor = parent.as_posix()
            if current.get(ancestor) != after.get(ancestor):
                conflicts.append(ancestor)
    # A new file inside an agent-created directory must never be removed by undo.
    for key in touched:
        if after.get(key, {}).get('kind') == 'dir' and before.get(key, {}).get('kind') != 'dir':
            conflicts.extend(k for k in current if k.startswith(key + '/') and k not in touched)
            # Include excluded and protected direct children without reading them.
            if current.get(key, {}).get('kind') == 'dir':
                for child in (root / key).iterdir():
                    name = child.relative_to(root).as_posix()
                    if name not in touched:
                        conflicts.append(name)
    if conflicts:
        raise ValueError('Undo refused: files changed after the session: ' + ', '.join(sorted(set(conflicts))))
    if dry_run:
        return touched
    # Make affected existing directories traversable until restoration finishes.
    for key in touched:
        if current.get(key, {}).get('kind') == 'dir':
            (root / key).chmod(current[key]['mode'] | 0o700)
    for key in sorted(touched, key=lambda k: (k.count('/'), k), reverse=True):
        path = root / key
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            # Keep directories when only their permissions changed.
            if before.get(key, {}).get('kind') != 'dir':
                path.rmdir()
    for key in sorted(touched, key=lambda k: (k.count('/'), k)):
        entry = before.get(key)
        if entry is None:
            continue
        path = root / key
        if entry['kind'] == 'dir':
            path.mkdir(mode=0o700, exist_ok=True)
        elif entry['kind'] == 'link':
            path.symlink_to(entry['target'])
        else:
            path.write_bytes(base64.b64decode(entry['data']))
        if entry['kind'] == 'file':
            path.chmod(entry['mode'])
    # Apply restrictive directory modes after restoring all children.
    for key in sorted(touched, key=lambda k: k.count('/'), reverse=True):
        if before.get(key, {}).get('kind') == 'dir':
            (root / key).chmod(before[key]['mode'])
    record['restored_paths'] = sorted(restored | set(touched))
    record['undone'] = len(record['restored_paths']) == len(original_changes)
    save(root, record)
