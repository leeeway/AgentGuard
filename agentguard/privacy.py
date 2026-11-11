"""Cooperative Python file-open policy; never reads protected contents."""
from fnmatch import fnmatchcase, translate
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
        canonical = path.resolve()
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


def _javascript_classes(body):
    # Python permits an unescaped closing bracket as the first class member;
    # JavaScript interprets it as the end of an empty class. Escape it explicitly.
    output = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == '\\':
            escaped = body[index + 1]
            output.append('\\' + escaped if escaped in r'^$\.*+?()[]{}|/' else escaped)
            index += 2
            continue
        if char != '[':
            output.append(char)
            index += 1
            continue
        output.append('[')
        index += 1
        if body[index:index + 1] == '^':
            output.append('^')
            index += 1
        if body[index:index + 1] == ']':
            output.append('\\]')
            index += 1
        while index < len(body) and body[index] != ']':
            char = body[index]
            if char == '\\':
                escaped = body[index + 1]
                output.append('\\' + escaped if escaped in r'^$\.*+?()[]{}|/-' else escaped)
                index += 2
            else:
                output.append('\\[' if char == '[' else char)
                index += 1
        output.append(']')
        index += 1
    return ''.join(output)


def node_rules(patterns):
    """Render Python glob syntax for JavaScript without accepting regex config."""
    validate(patterns)
    result = []
    for pattern in patterns:
        home = pattern.startswith('~/')
        translated = translate(pattern[2:] if home else pattern)
        # fnmatch uses a dot-all group, strict end anchor, and optional atomic
        # groups to optimize interior stars. JS uses the same glob language
        # with ordinary noncapturing groups; strict end must allow no newline.
        if not translated.startswith('(?s:') or translated[-3:] not in (')\\Z', ')\\z'):
            raise ValueError('Unsupported glob translation for Node.js protection')
        body = _javascript_classes(translated[4:-3].replace('(?>', '(?:'))
        result.append({'body': body, 'home': home, 'absolute': home or os.path.isabs(pattern)})
    return result
