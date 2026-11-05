"""Local session snapshots. This module is not an operating-system sandbox."""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid

from . import validation, objects

try:
    import tomllib
except ImportError:
    import tomli as tomllib

DEFAULT_CONFIG = '''[safety]
dangerous_commands = "block"
blocked_patterns = ['\\brm\\b.*(?:-[a-zA-Z]*[rf]|--recursive|--force)', '\\bgit\\s+push\\b.*(?:--force|-f)', '\\bgit\\s+reset\\s+--hard', '\\bmkfs', '\\bdd\\s+if=', '>\\s*/etc/']

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
    for section in ('safety', 'snapshots'):
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


def capture(root):
    result = {}
    def visit(directory):
        for path in sorted(directory.iterdir()):
            if path.name in ('.git', '.agentguard'):
                continue
            key = path.relative_to(root).as_posix()
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if path.is_symlink():
                result[key] = {'kind': 'link', 'target': os.readlink(path)}
            elif stat.S_ISDIR(info.st_mode):
                result[key] = {'kind': 'dir', 'mode': mode}
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                result[key] = {'kind': 'file', 'mode': mode, 'data': base64.b64encode(path.read_bytes()).decode('ascii')}
            else:
                raise ValueError(f'Unsupported special file: {key}')
    visit(root)
    return result


def snapshot_directory(root):
    directory = store(root) / 'snapshots'
    if directory.is_symlink():
        raise ValueError('Snapshot directory must not be a symbolic link')
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


def records(root):
    result = []
    for path in sorted(snapshot_directory(root).glob('*.json')):
        if path.is_symlink() or not path.is_file():
            raise ValueError('Snapshot record must be a regular file')
        try:
            record = objects.unpack(root, json.loads(path.read_text()))
            validation.record(record)
            if path.stem != record['id']:
                raise ValueError('Snapshot filename does not match its identifier')
        except ValueError as exc:
            raise ValueError(f'Invalid snapshot {path.name}: {exc}') from exc
        result.append(record)
    return result


def changes(before, after):
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def restore(root, record, dry_run=False):
    validation.record(record)
    if 'after' not in record:
        raise ValueError('Session is incomplete; automatic undo cannot safely check for later edits.')
    before, after = record['before'], record['after']
    current = capture(root)
    touched = changes(before, after)
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
    record['undone'] = True
    save(root, record)
