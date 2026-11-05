"""Loaded by Python's site machinery when runtime hooks are requested."""
import os

if os.environ.get('AGENTGUARD_POLICY'):
    try:
        from agentguard.runtime import install
        install()
    except BaseException:
        # Python normally ignores sitecustomize errors. Never silently start unguarded.
        os.write(2, b'AgentGuard: Python hook initialization failed.\n')
        os._exit(126)
