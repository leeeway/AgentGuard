"""Append-only cooperative decision logs. Command arguments are never accepted."""
from collections import deque
from datetime import datetime, timezone
import json
import os
import re


def path_for(root, session):
    from .core import store
    if not re.fullmatch(r'[0-9]{20}-[0-9a-f]{32}', session):
        raise ValueError('Invalid audit session identifier')
    directory = store(root) / 'audit'
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('Audit storage must be a directory, not a symbolic link')
    return directory / (session + '.jsonl')


def initialize(root, session):
    path = path_for(root, session)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return path


def append(path, source, event, decision, rules):
    row = {'timestamp': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid(),
           'source': source, 'event': event, 'decision': decision, 'rules': rules}
    data = (json.dumps(row, separators=(',', ':')) + '\n').encode()
    fd = os.open(path, os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW)
    try:
        if os.write(fd, data) != len(data):
            raise OSError('Incomplete audit write')
    finally:
        os.close(fd)


def read(root, session, limit):
    path = path_for(root, session)
    if path.is_symlink():
        raise ValueError('Audit log must not be a symbolic link')
    if not path.exists():
        return []
    rows = deque(maxlen=limit)
    with path.open() as stream:
        for index, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or set(row) != {'timestamp', 'pid', 'source', 'event', 'decision', 'rules'}
                        or row['source'] not in ('cli', 'python', 'node')
                        or row['decision'] not in ('allowed', 'warning', 'blocked', 'confirmed')
                        or type(row['pid']) is not int or not isinstance(row['rules'], list)
                        or any(type(rule) is not int or rule < 1 for rule in row['rules'])
                        or not isinstance(row['event'], str) or not isinstance(row['timestamp'], str)):
                    raise ValueError('Invalid event')
            except ValueError as exc:
                raise ValueError(f'Invalid audit event at line {index}') from exc
            rows.append(row)
    return list(rows)
