"""Bounded noninteractive validation processes on macOS/Linux."""
import os
import signal
import subprocess


def stop_group(process):
    def send(sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    send(signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Also remove same-group descendants after the leader exits.
        send(signal.SIGKILL)
        process.wait()


def verify(command, env, timeout=None, lock_fd=None):
    descriptors = () if lock_fd is None else (lock_fd,)
    if timeout is None:
        return subprocess.run(command, check=False, env=env, pass_fds=descriptors).returncode, False
    process = subprocess.Popen(command, env=env, start_new_session=True, stdin=subprocess.DEVNULL, pass_fds=descriptors)
    try:
        return process.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        stop_group(process)
        return 124, True
    except BaseException:
        stop_group(process)
        raise
