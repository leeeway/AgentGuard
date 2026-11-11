"""Read-only health checks; never repair, delete locks, or disclose file contents."""
from . import core, objects, locking


def inspect(root):
    checks = []
    def add(code, level, message, action=None, session=None, **details):
        row = {'code': code, 'level': level, 'message': message}
        if action:
            row['action'] = action
        if session:
            row['session'] = session
        row.update(details)
        checks.append(row)

    try:
        folder = core.store(root)
    except (OSError, ValueError) as exc:
        add('workspace-storage', 'error', str(exc), 'Inspect .agentguard without following its link.')
        return result(checks)
    try:
        settings = core.config(root)
        add('configuration', 'ok', 'Configuration is valid.')
        if not settings.get('safety', {}).get('blocked_patterns', []):
            add('empty-policy', 'warning', 'No launch command rules are configured.', 'Configure safety.blocked_patterns if command checks are intended.')
    except (OSError, ValueError) as exc:
        add('configuration', 'error', str(exc), 'Initialize the workspace or correct .agentguard/config.toml.')
    try:
        lock = locking.status(folder)
        state = lock['state']
        if state == 'active':
            add('workspace-lock', 'warning', 'A live operation or direct child holds the kernel workspace lock.',
                'Wait for it to finish; do not remove lock files.', state=state)
        elif state == 'recoverable':
            add('workspace-lock', 'warning', 'The kernel lock is free; a recognized interrupted-session marker remains.',
                'The next run, undo or gc will reclaim this marker. Inspect incomplete sessions separately.', state=state)
        elif state == 'legacy':
            add('workspace-lock', 'warning', 'A legacy or unrecognized session marker remains; ownership cannot be established.',
                'Remove only session.lock after confirming all old sessions and child processes have stopped.', state=state)
        else:
            add('workspace-lock', 'ok', 'Workspace kernel lock is free.', state=state)
    except (OSError, ValueError) as exc:
        add('workspace-lock', 'error', str(exc), 'Inspect lock file paths without deleting an active workspace.lock.')
    try:
        objects.directory(root)
        add('object-storage', 'ok', 'Object storage path is valid; referenced contents are checked below.')
    except (OSError, ValueError) as exc:
        add('object-storage', 'error', str(exc), 'Inspect the object storage path; do not run garbage collection until repaired.')
    try:
        paths = sorted(core.snapshot_directory(root).glob('*.json'))
    except (OSError, ValueError) as exc:
        add('snapshot-storage', 'error', str(exc), 'Inspect .agentguard/snapshots.')
        return result(checks)
    for path in paths:
        try:
            record = core.load_record(root, path)
        except (OSError, ValueError) as exc:
            add('snapshot-integrity', 'error', str(exc), 'Preserve the record and restore missing or damaged data from a trusted backup; no automatic repair was attempted.', path.name)
            continue
        add('snapshot-integrity', 'ok', 'Manifest and referenced contents validated.', session=record['id'])
        if 'after' not in record:
            add('incomplete-session', 'warning', 'This session has no final snapshot.', 'Preserve its before snapshot; ordinary undo and automatic recovery cannot safely restore this session.', record['id'])
        if record.get('automatic_rollback') in ('pending', 'failed'):
            add('unfinished-recovery', 'warning', 'Automatic recovery is not confirmed complete.', 'Inspect the workspace and session diff; if complete, use undo --dry-run before attempting recovery.', record['id'])
    if not paths:
        add('snapshot-inventory', 'ok', 'No recorded sessions.')
    return result(checks)


def result(checks):
    errors = sum(row['level'] == 'error' for row in checks)
    warnings = sum(row['level'] == 'warning' for row in checks)
    return {'status': 'error' if errors else 'warning' if warnings else 'ok',
            'errors': errors, 'warnings': warnings, 'checks': checks}
