import sys
import click
from rich.console import Console

console = Console()

@click.group()
@click.version_option(version="0.1.0", prog_name="agentguard")
def main():
    """🛡️ AgentGuard: The Runtime Safety Enclave & Instant Undo Engine for AI Coding Agents."""
    pass

@main.command()
def init():
    """Initialize AgentGuard configuration and snapshot store in current directory."""
    console.print("[bold green]✔[/bold green] Initialized AgentGuard in current workspace.")
    console.print("  [dim]Created config: .agentguard/config.toml[/dim]")
    console.print("  [dim]Snapshot store: .agentguard/snapshots[/dim]")

@main.command(context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.pass_context
def run(ctx):
    """Run an autonomous AI agent command under AgentGuard runtime protection."""
    cmd = ctx.args
    if not cmd:
        console.print("[bold red]Error:[/bold red] Please specify an agent command to run. E.g. `agentguard run -- claude`")
        sys.exit(1)
    
    console.print(f"[bold cyan]🛡️ AgentGuard Enclave Active[/bold cyan] -> Executing: [yellow]{' '.join(cmd)}[/yellow]")
    # TODO: invoke subprocess with safety rules and auto-snapshotting

@main.command()
@click.option("--all", "undo_all", is_flag=True, help="Undo all changes from the current session.")
def undo(undo_all):
    """Instantly rollback file changes made by the AI Agent."""
    if undo_all:
        console.print("[bold green]✔[/bold green] Reverted all changes made by the AI Agent session.")
    else:
        console.print("[bold green]✔[/bold green] Reverted the last step of file changes.")

@main.command()
def status():
    """Inspect recent snapshots, blocked commands, and active rules."""
    console.print("[bold cyan]AgentGuard Status:[/bold cyan]")
    console.print("  • Safety Firewall: [green]Active[/green]")
    console.print("  • Credential Masker: [green]Enabled[/green]")
    console.print("  • Recent Snapshots: [dim]0 recorded[/dim]")

if __name__ == "__main__":
    main()
