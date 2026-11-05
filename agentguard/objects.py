"""Content-addressed snapshot objects; manifests remain backward compatible."""
import base64
import copy
import hashlib
import os
import re
import uuid


def directory(root):
    from .core import store
    path = store(root) / 'objects'
    if path.is_symlink():
        raise ValueError('Object directory must not be a symbolic link')
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(root, name):
    if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{64}', name):
        raise ValueError('Invalid snapshot object identifier')
    path = directory(root) / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or unsafe snapshot object: ' + name)
    data = path.read_bytes()
    if digest(data) != name:
        raise ValueError('Snapshot object checksum mismatch: ' + name)
    return data


def write(root, data):
    name = digest(data)
    folder = directory(root)
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / name
    if path.exists() or path.is_symlink():
        read(root, name)
        return name
    temporary = folder / (uuid.uuid4().hex + '.tmp')
    try:
        with open(temporary, 'xb', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return name


def pack(root, record):
    result = copy.deepcopy(record)
    result['format_version'] = 2
    for field in ('before', 'after'):
        for entry in result.get(field, {}).values():
            if entry['kind'] == 'file':
                entry['object'] = write(root, base64.b64decode(entry.pop('data'), validate=True))
    return result


def unpack(root, record):
    if not isinstance(record, dict):
        raise ValueError('Snapshot record must be an object')
    version = record.get('format_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported snapshot format version')
    result = copy.deepcopy(record)
    if version == 2:
        for field in ('before', 'after'):
            manifest = result.get(field, {})
            if not isinstance(manifest, dict):
                raise ValueError('Snapshot manifest must be an object')
            for entry in manifest.values():
                if isinstance(entry, dict) and entry.get('kind') == 'file':
                    if 'data' in entry:
                        raise ValueError('Version 2 file must reference a snapshot object')
                    entry['data'] = base64.b64encode(read(root, entry.pop('object', None))).decode('ascii')
    return result


def collect(root, dry_run=True):
    from .core import records
    # Load and validate every retained record before deleting anything.
    referenced = set()
    for record in records(root):
        for field in ('before', 'after'):
            for entry in record.get(field, {}).values():
                if entry['kind'] == 'file':
                    referenced.add(digest(base64.b64decode(entry['data'], validate=True)))
    candidates = []
    folder = directory(root)
    if folder.exists():
        for path in sorted(folder.iterdir()):
            if re.fullmatch(r'[0-9a-f]{64}', path.name) and path.name not in referenced:
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Unsafe snapshot object encountered during cleanup')
                candidates.append((path, path.stat().st_size))
    if not dry_run:
        for path, _ in candidates:
            path.unlink()
    return len(candidates), sum(size for _, size in candidates)
