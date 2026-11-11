"""Kernel-backed cooperative workspace locks for macOS/Linux."""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
import re
import stat
import uuid


def _open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('Workspace lock paths must be regular files')
    return fd


def _marker(directory):
    try:
        fd = _open(directory / 'session.lock', os.O_RDONLY)
    except FileNotFoundError:
        return 'none', None
    with os.fdopen(fd) as stream:
        try:
            value = json.loads(stream.read(4097))
        except (ValueError, UnicodeError):
            return 'legacy', None
    if (isinstance(value, dict) and set(value) == {'version', 'pid', 'token'}
            and type(value['version']) is int and value['version'] == 2
            and type(value['pid']) is int and value['pid'] > 0
            and isinstance(value['token'], str) and re.fullmatch(r'[0-9a-f]{32}', value['token'])):
        return 'current', value
    return 'legacy', None


def _acquire(fd, operation):
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


@contextmanager
def workspace_lock(directory):
    if directory.is_symlink():
        raise ValueError('Workspace storage must not be a symbolic link')
    guard = directory / 'workspace.lock'
    had_guard = guard.exists()
    fd = _open(guard, os.O_CREAT | os.O_RDWR)
    token = uuid.uuid4().hex
    owned = False
    try:
        if not _acquire(fd, fcntl.LOCK_EX):
            raise ValueError('Workspace is locked by an active AgentGuard operation or child process.')
        kind, _ = _marker(directory)
        if kind == 'legacy' or (kind == 'current' and not had_guard):
            raise ValueError('Workspace is locked by a legacy or unrecognized session marker. '
                             'Run doctor; remove session.lock only after confirming all sessions have stopped.')
        if kind == 'current':
            # Exclusive kernel ownership proves the previous holder and every
            # child that retained this descriptor have released it.
            (directory / 'session.lock').unlink()
        marker = {'version': 2, 'pid': os.getpid(), 'token': token}
        marker_fd = _open(directory / 'session.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(marker_fd, 'w') as stream:
            json.dump(marker, stream)
            stream.flush()
            os.fsync(stream.fileno())
        owned = True
        yield fd
    finally:
        try:
            if owned:
                kind, value = _marker(directory)
                if kind == 'current' and value['token'] == token:
                    (directory / 'session.lock').unlink()
        finally:
            # Closing instead of LOCK_UN preserves the shared open-description
            # lock if a direct command still holds an inherited descriptor.
            os.close(fd)


def status(directory):
    """Probe existing locks without creating, deleting or rewriting files."""
    guard = directory / 'workspace.lock'
    try:
        fd = _open(guard, os.O_RDONLY)
    except FileNotFoundError:
        kind, _ = _marker(directory)
        return {'state': 'idle' if kind == 'none' else 'legacy'}
    try:
        if not _acquire(fd, fcntl.LOCK_SH):
            return {'state': 'active'}
        kind, value = _marker(directory)
        if kind == 'current':
            return {'state': 'recoverable', 'previous_pid': value['pid']}
        return {'state': 'idle' if kind == 'none' else 'legacy'}
    finally:
        os.close(fd)
