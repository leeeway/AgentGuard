"""Content-addressed snapshot objects; manifests remain backward compatible."""
import base64
import copy
import hashlib
import os
import re
import uuid

from .validation import SNAPSHOT_FIELDS


def directory(root):
    from .core import store
    path = store(root) / 'objects'
    if path.is_symlink():
        raise ValueError('Object directory must not be a symbolic link')
    if path.exists() and not path.is_dir():
        raise ValueError('Object storage must be a directory')
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def object_path(folder, name):
    if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{64}', name):
        raise ValueError('Invalid snapshot object identifier')
    path = folder / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or unsafe snapshot object: ' + name)
    return path


def read(root, name):
    data = object_path(directory(root), name).read_bytes()
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
    # Older collectors would miss content referenced only by the checkpoint.
    result['format_version'] = 3 if 'before_verification' in record else 2
    for field in SNAPSHOT_FIELDS:
        for entry in result.get(field, {}).values():
            if entry['kind'] == 'file':
                entry['object'] = write(root, base64.b64decode(entry.pop('data'), validate=True))
    return result


def format_version(record):
    if not isinstance(record, dict):
        raise ValueError('Snapshot record must be an object')
    version = record.get('format_version', 1)
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError('Unsupported snapshot format version')
    if (version == 3) != ('before_verification' in record):
        raise ValueError('Snapshot format does not match the verification checkpoint')
    return version


def unpack(root, record):
    version = format_version(record)
    result = copy.deepcopy(record)
    if version >= 2:
        for field in SNAPSHOT_FIELDS:
            manifest = result.get(field, {})
            if not isinstance(manifest, dict):
                raise ValueError('Snapshot manifest must be an object')
            for entry in manifest.values():
                if isinstance(entry, dict) and entry.get('kind') == 'file':
                    if 'data' in entry:
                        raise ValueError('Snapshot file must reference a content object')
                    entry['data'] = base64.b64encode(read(root, entry.pop('object', None))).decode('ascii')
    return result


def check_references(root, manifests, *, verify=False):
    """Check each distinct object once, streaming bytes when verifying hashes."""
    names = {
        entry['object']
        for record in manifests if format_version(record) >= 2
        for field in SNAPSHOT_FIELDS
        for entry in record.get(field, {}).values()
        if entry['kind'] == 'file'
    }
    if not names:
        return names
    folder = directory(root)
    for name in sorted(names):
        path = object_path(folder, name)
        if verify:
            checksum = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(chunk)
            if checksum.hexdigest() != name:
                raise ValueError('Snapshot object checksum mismatch: ' + name)
    return names


def collect(root, dry_run=True):
    from .core import records
    history = records(root, contents=False)
    referenced = check_references(root, history, verify=True)
    for record in history:
        if format_version(record) != 1:
            continue
        for field in SNAPSHOT_FIELDS:
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
