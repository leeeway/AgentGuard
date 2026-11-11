from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import shlex
from pathlib import Path
import subprocess
import time
import uuid

import click

from . import core, runtime, inspection, objects, inventory, diagnostics, processes, audit, privacy


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
@click.option('--protect-files', is_flag=True, help='Block protected paths in enabled Python/Node.js hooks.')
@click.option('--node-hooks', is_flag=True, help='Inject cooperative child_process checks into Node.js.')
@click.option('--rollback-on-failure', is_flag=True, help='Restore this session when the command exits nonzero.')
@click.option('--verify', multiple=True, help='Validation command after success; repeat to run checks in order.')
@click.option('--verify-timeout', type=click.FloatRange(min=0, min_open=True), help='Maximum seconds per verification command (noninteractive).')
@click.argument('command', nargs=-1, type=click.UNPROCESSED, required=True)
def run(command, python_hooks, node_hooks, rollback_on_failure, verify, verify_timeout, protect_files):
    """Check a launch command, snapshot the workspace, and execute it."""
    if protect_files and not (python_hooks or node_hooks):
        raise click.UsageError('--protect-files requires --python-hooks or --node-hooks.')
    if verify_timeout is not None and (not math.isfinite(verify_timeout) or not verify):
        raise click.UsageError('--verify-timeout requires --verify and a finite positive duration.')
    root = Path.cwd()
    settings = core.config(root)
    if protect_files and not settings.get('privacy', {}).get('protected_files', privacy.DEFAULT_PROTECTED):
        raise click.ClickException('--protect-files requires at least one privacy.protected_files rule.')
    directory = core.store(root)
    # Exclusive lock prevents concurrent sessions and undo in this workspace.
    with workspace_lock(directory):
        validators = []
        for index, text in enumerate(verify, 1):
            try:
                parsed = shlex.split(text)
            except ValueError as exc:
                raise click.ClickException(f'Invalid quoting in verification command {index}.') from exc
            if not parsed or not parsed[0]:
                raise click.ClickException(f'Verification command {index} must not be empty.')
            validators.append(parsed)
        # Check all launch commands before allowing the agent to modify files.
        for label, argv in [('Launch command', command)] + [
            (f'Verification command {i}', argv) for i, argv in enumerate(validators, 1)
        ]:
            evaluation = core.evaluate(argv, settings)
            if evaluation['matched_rules']:
                mode = evaluation['mode']
                rules = ', '.join(map(str, evaluation['matched_rules']))
                if mode == 'block':
                    raise click.ClickException(f'{label} blocked by safety rules: {rules}.')
                if mode == 'ask' and not click.confirm(f'{label} matches safety rules. Continue?', default=False):
                    raise click.ClickException('Launch cancelled.')
                click.echo(f'Warning: {label} matches safety rules.', err=True)
        click.echo('Session snapshots enabled. ' + ('Protected-file access checks enabled for selected runtime hooks.' if protect_files else 'Protected-file blocking is disabled.'), err=True)
        record = {
            'id': f'{time.time_ns():020d}-{uuid.uuid4().hex}',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'file_protection': protect_files,
            'hooks': [name for name, enabled in [('python', python_hooks), ('node', node_hooks)] if enabled],
            'automatic_rollback': 'pending' if rollback_on_failure else 'disabled',
            'verification': {'requested': len(validators), 'exit_codes': [], 'state': 'pending' if validators else 'disabled'},
            'snapshot_excludes': list(settings.get('snapshots', {}).get('exclude', [])),
            'before': core.capture(root, settings),
        }
        if verify_timeout is not None:
            record['verification']['timeout_seconds'] = verify_timeout
            record['verification']['timed_out'] = []
        core.save(root, record)
        audit_path = audit.initialize(root, record['id'])
        decision = core.evaluate(command, settings)
        audit.append(audit_path, 'cli', 'launch', 'confirmed' if decision['decision'] == 'confirmation-required' else decision['decision'], decision['matched_rules'])
        env = runtime.environment(settings, python_hooks, node_hooks, audit_path=audit_path, protected_root=root if protect_files else None)
        try:
            result = subprocess.run(command, check=False, env=env)
            code = result.returncode
            record['command_exit_code'] = code
            if validators:
                record['verification']['state'] = 'skipped' if code != 0 else 'passed'
            if code == 0:
                for index, argv in enumerate(validators, 1):
                    click.echo(f'Running verification {index}/{len(validators)}.', err=True)
                    decision = core.evaluate(argv, settings)
                    audit.append(audit_path, 'cli', f'verification.{index}', 'confirmed' if decision['decision'] == 'confirmation-required' else decision['decision'], decision['matched_rules'])
                    try:
                        code, timed_out = processes.verify(argv, env, verify_timeout)
                        if timed_out:
                            record['verification']['timed_out'].append(index)
                            click.echo(f'Verification command {index} timed out; process group stopped.', err=True)
                    except OSError:
                        code = 127
                        click.echo(f'Verification command {index} could not be started.', err=True)
                    record['verification']['exit_codes'].append(code)
                    if code != 0:
                        record['verification']['state'] = 'failed'
                        break
            record['exit_code'] = code
            if rollback_on_failure and code == 0:
                record['automatic_rollback'] = 'not-needed'
        except OSError:
            if rollback_on_failure:
                record['automatic_rollback'] = 'not-started'
            raise
        finally:
            record['finished_at'] = datetime.now(timezone.utc).isoformat()
            record['after'] = core.capture(root, settings)
            core.save(root, record)
        if rollback_on_failure and code != 0:
            try:
                core.restore(root, record)
            except (OSError, ValueError) as exc:
                record['automatic_rollback'] = 'failed'
                core.save(root, record)
                raise click.ClickException(
                    f'Command exited {code}; automatic rollback failed: {exc}. '
                    'Inspect history and workspace before retrying undo.'
                ) from exc
            record['automatic_rollback'] = 'restored'
            core.save(root, record)
            click.echo(f'Command exited {code}; session changes restored.', err=True)
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
@click.option('--path', 'paths', multiple=True, help='Restore only this exact relative file/link path; repeatable.')
def undo(undo_all, dry_run, paths):
    """Undo the latest session, refusing to overwrite subsequent edits."""
    if undo_all and paths:
        raise click.UsageError('--all and --path cannot be combined.')
    root = Path.cwd()
    core.config(root)
    with workspace_lock(core.store(root)):
        pending = [r for r in core.records(root) if not r.get('undone')]
        if not pending:
            click.echo('No sessions to undo.')
            return
        touched = core.restore(root, pending[-1], dry_run=dry_run, paths=paths)
        if dry_run:
            for path in touched:
                click.echo('would restore: ' + json.dumps(path, ensure_ascii=False))
            click.echo(f'Undo preview: {len(touched)} paths; no files changed.')
            return
    click.echo('Restored selected paths.' if paths else 'Restored the latest session.')


@main.command()
def status():
    """Show changes recorded in the latest session."""
    root = Path.cwd()
    core.config(root)
    history = core.records(root)
    click.echo(f'Sessions: {len(history)}')
    click.echo('Protection: launch-command checks; optional Python/Node command hooks; file access checks via --protect-files.')
    pending = [r for r in history if not r.get('undone')]
    if pending:
        record = pending[-1]
        if 'after' not in record:
            click.echo('Latest session is running or incomplete.')
            return
        for key in core.changes(record['before'], record['after']):
            if key in record.get('restored_paths', []):
                continue
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
        click.echo(f"{row['id']}  {row['state']}  exit={code}  changed={changed}  recovery={row['automatic_rollback']}  verification={row['verification']['state']}")


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


@main.command('gc')
@click.option('--dry-run', is_flag=True, help='Preview unreferenced content without deleting it.')
def garbage_collect(dry_run):
    """Remove content objects no longer referenced by retained sessions."""
    root = Path.cwd()
    core.config(root)
    with workspace_lock(core.store(root)):
        count, size = objects.collect(root, dry_run=dry_run)
    action = 'Would remove' if dry_run else 'Removed'
    click.echo(f'{action} {count} unreferenced objects ({size} bytes).')


@main.command('check', context_settings={'ignore_unknown_options': True})
@click.option('--json', 'as_json', is_flag=True, help='Emit the decision and matching rule numbers as JSON.')
@click.argument('command', nargs=-1, type=click.UNPROCESSED, required=True)
def check_command(as_json, command):
    """Explain launch rules without executing a command or creating a snapshot."""
    result = core.evaluate(command, core.config(Path.cwd()))
    if as_json:
        click.echo(json.dumps(result))
    else:
        rules = ', '.join(map(str, result['matched_rules'])) or 'none'
        click.echo(f"Decision: {result['decision']}; mode: {result['mode']}; matched rules: {rules}")
        click.echo(f"Scope: launch command only; {result['rule_count']} configured rules. Command was not executed.")
    raise click.exceptions.Exit({'blocked': 3, 'confirmation-required': 4}.get(result['decision'], 0))


@main.command('snapshot-plan')
@click.option('--json', 'as_json', is_flag=True, help='Emit inventory and budget violations as JSON.')
def snapshot_plan(as_json):
    """Estimate snapshot input size without reading file contents or writing a snapshot."""
    root = Path.cwd()
    result = inventory.plan(root, core.config(root))
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo(f"Files: {result['files']}; directories: {result['directories']}; symlinks: {result['symlinks']}")
        click.echo(f"Input bytes: {result['total_bytes']}; ready: {result['ready']}")
        if result['largest_file']:
            click.echo('Largest file: ' + json.dumps(result['largest_file'], ensure_ascii=False))
        for issue in result['issues']:
            click.echo('Issue: ' + json.dumps(issue, ensure_ascii=False))
    raise click.exceptions.Exit(0 if result['ready'] else 3)


@main.command('doctor')
@click.option('--json', 'as_json', is_flag=True, help='Emit structured health checks and suggested actions.')
def doctor(as_json):
    """Diagnose configuration and retained snapshots without changing files."""
    report = diagnostics.inspect(Path.cwd())
    if as_json:
        click.echo(json.dumps(report, ensure_ascii=False))
    else:
        click.echo(f"Health: {report['status']}; errors: {report['errors']}; warnings: {report['warnings']}")
        for row in report['checks']:
            # JSON quoting prevents filenames and error messages injecting terminal controls.
            click.echo(f"[{row['level']}] {row['code']}: " + json.dumps(row['message'], ensure_ascii=True))
            if 'session' in row:
                click.echo('  Session: ' + json.dumps(row['session'], ensure_ascii=True))
            if 'action' in row:
                click.echo('  ' + row['action'])
    raise click.exceptions.Exit(1 if report['errors'] else 3 if report['warnings'] else 0)


@main.command('audit')
@click.option('--session', help='Full session ID; defaults to the latest retained session.')
@click.option('--json', 'as_json', is_flag=True, help='Emit recorded decisions as JSON.')
@click.option('--limit', type=click.IntRange(min=1), default=100, show_default=True)
def audit_command(session, as_json, limit):
    """Read the last recorded decisions for a session in chronological order."""
    root = Path.cwd()
    core.config(root)
    if session is None:
        session = inspection.select(core.records(root))['id']
    rows = audit.read(root, session, limit)
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=True))
    elif not rows:
        click.echo('No recorded audit events.')
    else:
        for row in rows:
            click.echo(json.dumps(row, ensure_ascii=True))


if __name__ == '__main__':
    main()
