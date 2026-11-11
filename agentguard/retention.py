"""Snapshot retention shared by automatic and explicit pruning."""


def reasons(record):
    protected = []
    if record.get('pinned', False):
        protected.append('pinned')
    if 'after' not in record:
        protected.append('incomplete')
    if not record.get('undone', False):
        if record.get('restored_paths'):
            protected.append('partially-restored')
        if record.get('automatic_rollback') in ('pending', 'failed'):
            protected.append('unfinished-recovery')
    return protected


def prune(root, maximum, dry_run=False):
    from .core import records, snapshot_directory
    if type(maximum) is not int or maximum < 1:
        raise ValueError('max_history must be a positive integer')
    # Validate every retained manifest and content object before deleting any.
    history = records(root)
    protected = []
    regular = []
    for record in history:
        keep = reasons(record)
        if keep:
            protected.append({'id': record['id'], 'reasons': keep})
        else:
            regular.append(record['id'])
    candidates = regular[:-maximum]
    if not dry_run:
        directory = snapshot_directory(root)
        for session in candidates:
            (directory / (session + '.json')).unlink()
    return {'max_history': maximum, 'total_sessions': len(history),
            'candidates': candidates, 'protected': protected,
            'kept_regular': regular[-maximum:], 'dry_run': dry_run,
            'removed_count': 0 if dry_run else len(candidates)}
