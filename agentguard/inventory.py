"""Metadata-only inventory shared by preflight and snapshot capture."""
import stat
from fnmatch import fnmatchcase


def entries(root, excludes=()):
    def visit(directory):
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            if path.name in ('.git', '.agentguard') or any(fnmatchcase(name, pattern) for pattern in excludes):
                continue
            info = path.lstat()
            yield path, info
            if stat.S_ISDIR(info.st_mode):
                yield from visit(path)
    yield from visit(root)


def plan(root, settings):
    limits = settings.get('snapshots', {})
    total = files = directories = links = 0
    largest = None
    issues = []
    for path, info in entries(root, limits.get('exclude', [])):
        name = path.relative_to(root).as_posix()
        if stat.S_ISREG(info.st_mode):
            files += 1
            total += info.st_size
            if largest is None or info.st_size > largest['bytes']:
                largest = {'path': name, 'bytes': info.st_size}
            if limits.get('max_file_bytes') is not None and info.st_size > limits['max_file_bytes']:
                issues.append({'reason': 'max_file_bytes', 'path': name, 'bytes': info.st_size})
        elif stat.S_ISDIR(info.st_mode):
            directories += 1
        elif stat.S_ISLNK(info.st_mode):
            links += 1
        else:
            issues.append({'reason': 'unsupported-file-type', 'path': name})
    if limits.get('max_total_bytes') is not None and total > limits['max_total_bytes']:
        issues.append({'reason': 'max_total_bytes', 'bytes': total})
    return {'files': files, 'directories': directories, 'symlinks': links,
            'total_bytes': total, 'largest_file': largest,
            'limits': {key: limits.get(key) for key in ('max_file_bytes', 'max_total_bytes')},
            'issues': issues, 'ready': not issues, 'exclude': limits.get('exclude', [])}
