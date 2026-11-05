from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import uuid

import click

from . import core, runtime, inspection


class GuardGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=GuardGroup)
@click.version_option(version='0.1.0', prog_name='agentguard')
def main():
    """Local command checks and session rollback for coding agents."""


@main.command()
def init():
    """Initialize the current workspace without overwriting configuration."""
    core.initialize(Path.cwd())
    click.echo('Initialized .agentguard/config.toml')


@main.command(context_settings={'ignore_unknown_options': True})
@click.option('--python-hooks', is_flag=True, help='Inject cooperative subprocess checks into Python interpreters.')
@click.option('--node-hooks', is_flag=True, help='Inject cooperative child_process checks into Node.js.')
@click.argument('command', nargs=-1, type=click.UNPROCESSED, required=True)
def run(command, python_hooks, node_hooks):
    """Check a launch command, snapshot the workspace, and execute it."""
    root = Path.cwd()
    settings = core.config(root)
    directory = core.store(root)
    # Exclusive lock prevents concurrent sessions and undo in this workspace.
    with workspace_lock(directory):
        if core.dangerous(command, settings):
            mode = settings.get('safety', {}).get('dangerous_commands', 'block')
            if mode == 'block':
                raise click.ClickException('Launch command blocked by safety rules.')
            if mode == 'ask' and not click.confirm('Potentially destructive launch command. Continue?', default=False):
                raise click.ClickException('Launch cancelled.')
            click.echo('Warning: potentially destructive launch command.', err=True)
        click.echo('Session snapshots enabled. ' + ('Opt-in runtime hooks enabled; secret reads are not intercepted.' if python_hooks or node_hooks else 'Child commands and secret reads are not intercepted.'), err=True)
        record = {
            'id': f'{time.time_ns():020d}-{uuid.uuid4().hex}',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'hooks': [name for name, enabled in [('python', python_hooks), ('node', node_hooks)] if enabled],
            'before': core.capture(root),
        }
        core.save(root, record)
        try:
            result = subprocess.run(command, check=False, env=runtime.environment(settings, python_hooks, node_hooks) if python_hooks or node_hooks else None)
            code = result.returncode
            record['exit_code'] = code
        finally:
            record['finished_at'] = datetime.now(timezone.utc).isoformat()
            record['after'] = core.capture(root)
            core.save(root, record)
        maximum = settings.get('snapshots', {}).get('max_history', 30)
        for old in core.records(root)[:-maximum]:
            (directory / 'snapshots' / (old['id'] + '.json')).unlink()
    raise click.exceptions.Exit(code if code >= 0 else 128 - code)


@contextmanager
def workspace_lock(directory):
    path = directory / 'session.lock'
    try:
        stream = path.open('x')
    except FileExistsError:
        raise click.ClickException('Workspace is locked. If no session is running, remove .agentguard/session.lock.')
    try:
        with stream:
            yield
    finally:
        path.unlink()


@main.command()
@click.option('--all', 'undo_all', is_flag=True, help='Restore the latest session (currently one snapshot per session).')
@click.option('--dry-run', is_flag=True, help='Check conflicts and preview paths without changing files.')
def undo(undo_all, dry_run):
    """Undo the latest session, refusing to overwrite subsequent edits."""
    root = Path.cwd()
    core.config(root)
    with workspace_lock(core.store(root)):
        pending = [r for r in core.records(root) if not r.get('undone')]
        if not pending:
            click.echo('No sessions to undo.')
            return
        touched = core.restore(root, pending[-1], dry_run=dry_run)
        if dry_run:
            for path in touched:
                click.echo('would restore: ' + json.dumps(path, ensure_ascii=False))
            click.echo(f'Undo preview: {len(touched)} paths; no files changed.')
            return
    click.echo('Restored the latest session.')


@main.command()
def status():
    """Show changes recorded in the latest session."""
    root = Path.cwd()
    core.config(root)
    history = core.records(root)
    click.echo(f'Sessions: {len(history)}')
    click.echo('Protection: launch-command checks; optional Python/Node hooks via run flags; no credential interception.')
    pending = [r for r in history if not r.get('undone')]
    if pending:
        record = pending[-1]
        if 'after' not in record:
            click.echo('Latest session is running or incomplete.')
            return
        for key in core.changes(record['before'], record['after']):
            label = 'added' if key not in record['before'] else 'deleted' if key not in record['after'] else 'modified'
            click.echo(f'{label}: {json.dumps(key, ensure_ascii=False)}')


@main.command('history')
@click.option('--json', 'as_json', is_flag=True, help='Emit machine-readable session metadata.')
@click.option('--limit', type=click.IntRange(min=1), default=20, show_default=True)
def history_command(as_json, limit):
    """List recorded sessions, newest first, including undone sessions."""
    root = Path.cwd()
    core.config(root)
    rows = [inspection.summary(r) for r in reversed(core.records(root))][:limit]
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        click.echo('No recorded sessions.')
    for row in rows:
        code = row['exit_code'] if row['exit_code'] is not None else 'unknown'
        changed = row['changed_paths'] if row['changed_paths'] is not None else 'unknown'
        click.echo(f"{row['id']}  {row['state']}  exit={code}  changed={changed}")


@main.command('diff')
@click.option('--session', help='Exact session ID from history; defaults to the latest session.')
@click.option('--contents', is_flag=True, help='Show text contents, which may include secrets.')
@click.option('--path', 'paths', multiple=True, help='Only show this recorded workspace-relative path; repeatable.')
def diff_command(session, contents, paths):
    """Inspect recorded before/after changes without reading current workspace files."""
    root = Path.cwd()
    core.config(root)
    record = inspection.select(core.records(root), session)
    output = inspection.diff(record, contents, paths)
    click.echo(output or 'No changes recorded.\n', nl=False)


if __name__ == '__main__':
    main()
