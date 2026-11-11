"""Cooperative Python file-open policy; never reads protected contents."""
from fnmatch import fnmatchcase
import os
from pathlib import Path

DEFAULT_PROTECTED = [
    '.env*', '**/.env*', '*.pem', '*.key',
    '~/.ssh', '~/.aws/credentials', '~/.config/gcloud',
]


def validate(patterns):
    if not isinstance(patterns, list) or any(
        not isinstance(p, str) or not p or '\x00' in p or '\\' in p
        or (p.startswith('~') and not p.startswith('~/'))
        or any(part in ('.', '..') for part in p.split('/'))
        for p in patterns
    ):
        raise ValueError('privacy.protected_files must be an array of nonempty path globs without dot or parent components')


class FilePolicy:
    def __init__(self, root, patterns):
        validate(patterns)
        self.root = Path(root).resolve()
        self.patterns = [os.path.expanduser(p) for p in patterns]

    def match(self, path):
        # Descriptor-based access is outside this path-based policy.
        if isinstance(path, int):
            return None
        path = Path(os.fsdecode(path))
        lexical = Path(os.path.abspath(path))
        # Test both the named path and its symlink target.
        canonical = lexical.resolve()
        absolute_names = set()
        relative_names = set()
        for candidate in (lexical, canonical):
            for ancestor in (candidate, *candidate.parents):
                absolute_names.add(ancestor.as_posix())
                try:
                    relative_names.add(ancestor.relative_to(self.root).as_posix())
                except ValueError:
                    pass
        for index, pattern in enumerate(self.patterns, 1):
            names = absolute_names if os.path.isabs(pattern) else relative_names
            if any(fnmatchcase(name, pattern) for name in names):
                return index
        return None
