"""Append-only cooperative decision logs. Command arguments are never accepted."""
from collections import deque
from datetime import datetime, timezone
import json
import os
import re
import stat


def directory(root):
    from .core import store
    path = store(root) / 'audit'
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError('Audit storage must be a directory, not a symbolic link')
    return path


def path_for(root, session):
    if not re.fullmatch(r'[0-9]{20}-[0-9a-f]{32}', session):
        raise ValueError('Invalid audit session identifier')
    return directory(root) / (session + '.jsonl')


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


def prune(root, keep_archived=30, dry_run=False):
    """Keep all snapshot-linked logs and the newest N archived session logs.

    Caller must hold the workspace lock. Log payloads are never read here.
    """
    from .core import records
    if type(keep_archived) is not int or keep_archived < 0:
        raise ValueError('Audit archive retention must be a nonnegative integer')
    # Invalid snapshots must not turn linked logs into deletion candidates.
    referenced = {record['id'] for record in records(root)}
    folder = directory(root)
    linked, archived = [], []
    ignored = 0
    if folder.exists():
        for path in sorted(folder.iterdir()):
            if not re.fullmatch(r'[0-9]{20}-[0-9a-f]{32}\.jsonl', path.name):
                ignored += 1
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Audit session logs must be regular files, not links or directories')
            item = {'session': path.stem, 'bytes': info.st_size}
            (linked if path.stem in referenced else archived).append(item)
    candidates = archived[:-keep_archived] if keep_archived else archived
    retained = archived[-keep_archived:] if keep_archived else []
    if not dry_run:
        for item in candidates:
            (folder / (item['session'] + '.jsonl')).unlink()
    return {'keep_archived': keep_archived, 'dry_run': dry_run,
            'candidates': candidates, 'candidate_bytes': sum(item['bytes'] for item in candidates),
            'snapshot_linked': linked, 'retained_archived': retained,
            'ignored_entries': ignored, 'removed_count': 0 if dry_run else len(candidates)}
