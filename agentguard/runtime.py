"""Opt-in CPython audit hooks; cooperative guardrails, not a sandbox."""
import json
import os
from pathlib import Path
import re
import sys


def environment(settings, python_hooks=True, node_hooks=False, audit_path=None):
    env = os.environ.copy()
    package = Path(__file__).resolve().parent
    if python_hooks:
        env['PYTHONPATH'] = os.pathsep.join(
            [str(package / 'hooks'), str(package.parent)]
            + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else [])
        )
    if node_hooks:
        preload = '--require=' + json.dumps(str(package / 'hooks' / 'node.cjs'))
        env['NODE_OPTIONS'] = preload + ' ' + env.get('NODE_OPTIONS', '')
    if audit_path is not None:
        env['AGENTGUARD_AUDIT'] = str(audit_path)
    else:
        env.pop('AGENTGUARD_AUDIT', None)
    env['AGENTGUARD_POLICY'] = json.dumps(settings.get('safety', {}))
    return env


def install():
    from .audit import append
    log_path = os.environ.get('AGENTGUARD_AUDIT')
    policy = json.loads(os.environ['AGENTGUARD_POLICY'])
    patterns = [re.compile(p) for p in policy.get('blocked_patterns', [])]
    mode = policy.get('dangerous_commands', 'block')
    if mode not in ('block', 'ask', 'warn'):
        raise ValueError('Invalid runtime policy')

    def flatten(value):
        if isinstance(value, (list, tuple)):
            return ' '.join(flatten(item) for item in value)
        if isinstance(value, bytes):
            return os.fsdecode(value)
        return str(value)

    def audit(event, args):
        if event == 'subprocess.Popen':
            command = flatten(args[:2])
        elif event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.spawn'):
            command = flatten(args[:1] if event == 'os.system' else args[:2])
        else:
            return
        matched = next((i for i, pattern in enumerate(patterns) if pattern.search(command)), None)
        action = 'allowed' if matched is None else 'warning' if mode == 'warn' else 'blocked'
        if log_path:
            append(log_path, 'python', event, action, [] if matched is None else [matched + 1])
        if matched is None:
            return
        # Do not put command arguments (which can include secrets) in diagnostics.
        action = 'warning' if mode == 'warn' else 'blocked'
        message = f'AgentGuard: {action} {event} by rule {matched + 1}'
        os.write(2, (message + '\n').encode())
        if mode != 'warn':
            # Runtime ask cannot safely share an agent's stdin; fail closed.
            raise PermissionError(message)

    sys.addaudithook(audit)
