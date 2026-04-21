"""Mayushii CLI — Typer-based interface for the orchestrator.

Commands:
  mayushii start                         — start orchestrator tmux session
  mayushii stop                          — stop everything
  mayushii status                        — show dashboard
  mayushii stalls                        — check for stalled workers
  mayushii worker start <id>             — launch worker
  mayushii worker send <id> <msg>        — message a worker
  mayushii worker list                   — show workers
  mayushii worker stop <id>              — stop worker
  mayushii worker output <id>            — capture worker output
  mayushii crew ask <id> <question>      — worker asks orchestrator
  mayushii hook session-start <id>       — called by SessionStart hook
  mayushii hook heartbeat <id>           — called by PostToolUse hook
  mayushii hook stop <id>                — called by Stop hook
  mayushii skill list                    — list available skills
  mayushii skill select <desc>           — LLM skill selection
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from mayushii.store import Store, MessageDirection
from mayushii import tmux, lifecycle, skills
from mayushii.hooks import handle_session_start, handle_heartbeat, handle_stop, _beads_env
from mayushii.lifecycle import validate_task_id, validate_model, validate_role, VALID_MODELS

app = typer.Typer(name="mayushii", help="Mayushii — AI Agent Orchestrator — beads + tmux + Claude Code")
worker_app = typer.Typer(name="worker", help="Manage worker agents")
skill_app = typer.Typer(name="skill", help="Manage skills")
hook_app = typer.Typer(name="hook", help="Hook callbacks (called by Claude Code, not you)")
crew_app = typer.Typer(name="crew", help="Inter-agent communication")
app.add_typer(worker_app, name="worker")
app.add_typer(skill_app, name="skill")
app.add_typer(hook_app, name="hook")
app.add_typer(crew_app, name="crew")

console = Console()
store = Store()


# ============================================================
# Top-level commands
# ============================================================

@app.command()
def start(
    session_name: str = typer.Option("mayushii", "--name", "-n", help="tmux session name"),
    prompt: str = typer.Option("", "--prompt", "-p", help="Initial prompt for orchestrator"),
    model: str = typer.Option("claude-opus-4-6", "--model", "-m", help="Claude model to use"),
    repo: str = typer.Option("", "--repo", "-r", help="Target repository path"),
    no_attach: bool = typer.Option(False, "--no-attach", help="Don't auto-attach to tmux"),
) -> None:
    """Start the orchestrator — creates tmux session, launches Claude Code, and attaches you."""
    validate_model(model)
    existing = store.get_active_orchestrator()
    if existing and tmux.session_exists(existing.tmux_session):
        console.print(f"[yellow]Orchestrator already running, attaching...[/]")
        subprocess.run(["tmux", "attach-session", "-t", existing.tmux_session])
        return

    default_repo_file = Path.home() / ".mayushii" / "default-repo"
    if repo:
        repo_path = Path(repo).resolve()
        # Remember for next time
        default_repo_file.parent.mkdir(parents=True, exist_ok=True)
        default_repo_file.write_text(str(repo_path))
    elif default_repo_file.exists():
        repo_path = Path(default_repo_file.read_text().strip())
    else:
        console.print("[red]No repo specified. Run with --repo /path/to/repo first time.[/]")
        console.print("[dim]Subsequent runs will remember the path.[/]")
        raise typer.Exit(1)

    if not repo_path.exists():
        console.print(f"[red]Repo path does not exist: {repo_path}[/]")
        raise typer.Exit(1)

    # 1. Inject orchestrator skill into the target repo's .claude/skills/
    orch_skill_src = repo_path / "orchestrator" / "SKILL.md"
    if orch_skill_src.exists():
        skills_dir = repo_path / ".claude" / "skills" / "orchestrator"
        skills_dir.mkdir(parents=True, exist_ok=True)
        skill_dst = skills_dir / "SKILL.md"
        if skill_dst.exists() or skill_dst.is_symlink():
            skill_dst.unlink()
        skill_dst.write_text(orch_skill_src.read_text())

    # Write orchestrator CLAUDE.md into the repo
    # (preserves any existing one by writing to a known location)
    orch_claude_md = repo_path / "CLAUDE.md"
    orch_skill_content = ""
    if orch_skill_src.exists():
        raw = orch_skill_src.read_text()
        parts = raw.split("---", 2)
        orch_skill_content = parts[2].strip() if len(parts) >= 3 else raw

    # Back up existing CLAUDE.md if present
    backup_path = repo_path / ".claude" / "CLAUDE.md.mayushii-backup"
    if orch_claude_md.exists():
        existing_content = orch_claude_md.read_text()
        if "You are Mayushii" not in existing_content:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(existing_content)

    orch_claude_md.write_text(
        "# Mayushii — AI Agent Orchestrator\n\n"
        "You are Mayushii, an AI orchestrator that coordinates worker agents.\n"
        "You have access to `bd` (beads) for task management and `mayushii` CLI for worker management.\n\n"
        f"## Target Repository\n"
        f"You are running in: `{repo_path}`\n"
        f"All `bd` commands work from this directory.\n"
        f"When launching workers, ALWAYS pass `--repo {repo_path}`.\n\n"
        f"{orch_skill_content}\n"
    )

    # 2. Create tmux session in the repo directory
    if tmux.session_exists(session_name):
        tmux.kill_session(session_name)
    tmux.create_session(session_name, first_window="orchestrator", cwd=str(repo_path))
    orch = store.create_orchestrator(session_name)
    target = f"{session_name}:orchestrator"

    # Wait for shell to be ready
    console.print("[dim]Waiting for shell...[/]")
    if not tmux.wait_for_ready(target, sentinel="$", timeout=10):
        tmux.wait_for_ready(target, sentinel="%", timeout=5)

    console.print(f"[dim]Launching Claude Code in {repo_path}...[/]")
    claude_cmd = f"claude --model {model} --dangerously-skip-permissions"
    tmux.send_command(target, claude_cmd)

    # Always wait for Claude Code to be ready before attaching or sending prompt
    tmux.wait_for_ready(target, timeout=30)

    if prompt:
        tmux.send_command(target, prompt)

    # 3. Attach directly
    if not no_attach:
        subprocess.run(["tmux", "attach-session", "-t", session_name])


@app.command()
def stop() -> None:
    """Stop the orchestrator and all workers."""
    orch = store.get_active_orchestrator()
    if not orch:
        console.print("[yellow]No active orchestrator.[/]")
        return

    sessions = store.list_running_sessions(orch.id)
    for session in sessions:
        lifecycle.stop_worker(store, session.task_id)
        console.print(f"  Stopped worker: {session.task_id}")

    tmux.kill_session(orch.tmux_session)
    store.stop_orchestrator(orch.id)

    # Restore backed-up CLAUDE.md if present
    default_repo_file = Path.home() / ".mayushii" / "default-repo"
    repo_path = Path(default_repo_file.read_text().strip()) if default_repo_file.exists() else None
    if not repo_path:
        console.print("[green]Orchestrator stopped.[/]")
        return
    backup_path = repo_path / ".claude" / "CLAUDE.md.mayushii-backup"
    claude_md = repo_path / "CLAUDE.md"
    if backup_path.exists():
        claude_md.write_text(backup_path.read_text())
        backup_path.unlink()
        console.print("[dim]Restored original CLAUDE.md[/]")

    # Clean up injected orchestrator skill
    injected_skill = repo_path / ".claude" / "skills" / "orchestrator"
    if injected_skill.exists():
        import shutil
        shutil.rmtree(injected_skill)

    console.print("[green]Orchestrator stopped.[/]")


@app.command()
def status() -> None:
    """Show orchestrator status dashboard."""
    orch = store.get_active_orchestrator()
    if not orch:
        console.print("[yellow]No active orchestrator.[/]")
        return

    lifecycle.refresh_worker_states(store, orch.id)
    sessions = store.list_sessions(orch.id)
    windows = tmux.list_windows(orch.tmux_session)

    table = Table(title=f"Mayushii — {orch.tmux_session}")
    table.add_column("Task ID", style="cyan")
    table.add_column("Role", style="green")
    table.add_column("Skills")
    table.add_column("Status")
    table.add_column("Idle", justify="right")

    for s in sessions:
        status_style = {
            "running": "[green]running[/]",
            "starting": "[yellow]starting[/]",
            "done": "[dim]done[/]",
            "failed": "[red]failed[/]",
            "stopped": "[dim]stopped[/]",
        }.get(s.status, s.status)

        idle = ""
        if s.status == "running":
            idle_s = int(s.idle_seconds)
            if idle_s > 600:
                idle = f"[red]{idle_s // 60}m[/]"
            elif idle_s > 60:
                idle = f"[yellow]{idle_s // 60}m[/]"
            else:
                idle = f"{idle_s}s"

        table.add_row(s.task_id, s.role, s.skills, status_style, idle)

    console.print(table)
    console.print(f"\n[dim]Tmux windows: {len(windows)} | Workers: {len(sessions)}[/]")


@app.command()
def stalls(
    threshold: int = typer.Option(10, "--threshold", "-t", help="Minutes of inactivity to consider stalled"),
) -> None:
    """Check for stalled workers and report them."""
    orch = store.get_active_orchestrator()
    if not orch:
        console.print("[yellow]No active orchestrator.[/]")
        return

    stale = store.list_stale_sessions(orch.id, threshold_minutes=threshold)
    if not stale:
        console.print("[green]No stalled workers.[/]")
        return

    console.print(f"[red]Found {len(stale)} stalled worker(s):[/]")
    for s in stale:
        idle_min = int(s.idle_seconds / 60)
        console.print(f"  {s.task_id} ({s.role}) — idle {idle_min}m")

    # Signal orchestrator about stalls
    for s in stale:
        idle_min = int(s.idle_seconds / 60)
        target = f"{orch.tmux_session}:orchestrator"
        try:
            tmux.send_command(target, f"[Worker {s.task_id}]: stalled — no activity for {idle_min} minutes")
        except Exception:
            pass


# ============================================================
# Worker commands
# ============================================================

@worker_app.command("start")
def worker_start(
    task_id: str = typer.Argument(help="Beads task ID (e.g. orch-9ak)"),
    role: str = typer.Option("explore", "--role", "-r", help="Agent role: explore|plan|edit|verify"),
    skill_names: str = typer.Option("", "--skills", "-s", help="Comma-separated skill names"),
    context: str = typer.Option("", "--context", "-c", help="Context from prior tasks"),
    prompt: str = typer.Option("", "--prompt", "-p", help="Custom initial prompt"),
    repo: str = typer.Option("", "--repo", help="Repository path to work in"),
    auto_skills: bool = typer.Option(False, "--auto-skills", help="LLM-select skills automatically"),
) -> None:
    """Launch a worker agent in a tmux window."""
    orch = store.get_active_orchestrator()
    if not orch:
        console.print("[red]No active orchestrator. Run 'mayushii start' first.[/]")
        raise typer.Exit(1)

    skill_list = [s.strip() for s in skill_names.split(",") if s.strip()]

    if auto_skills and not skill_list:
        catalog = skills.load_catalog()
        if catalog:
            skill_list = skills.select_skills_via_llm(
                task_description=f"Task {task_id}, role: {role}",
                role=role,
                catalog=catalog,
            )
            console.print(f"[dim]Auto-selected skills: {', '.join(skill_list) or 'none'}[/]")

    session = lifecycle.start_worker(
        store=store,
        orchestrator_id=orch.id,
        orch_session=orch.tmux_session,
        task_id=task_id,
        role=role,
        skills=skill_list,
        context=context,
        prompt=prompt or None,
        repo_path=repo or None,
    )

    console.print(Panel(
        f"[bold green]Worker launched[/]\n\n"
        f"Task: {task_id}\n"
        f"Role: {role}\n"
        f"Skills: {', '.join(skill_list) or 'none'}\n"
        f"Window: {session.window_name}",
        title="Worker Started",
    ))


@worker_app.command("stop")
def worker_stop(
    task_id: str = typer.Argument(help="Task ID of worker to stop"),
) -> None:
    """Stop a worker agent."""
    lifecycle.stop_worker(store, task_id)
    console.print(f"[green]Worker {task_id} stopped.[/]")


@worker_app.command("send")
def worker_send(
    task_id: str = typer.Argument(help="Task ID of target worker"),
    message: str = typer.Argument(help="Message content"),
    msg_type: str = typer.Option("nudge", "--type", "-t", help="Message type: nudge|status|normal|divert"),
) -> None:
    """Send a message to a worker."""
    valid_types = {"nudge", "status", "normal", "divert"}
    if msg_type not in valid_types:
        console.print(f"[red]Invalid message type '{msg_type}'. Must be one of: {', '.join(sorted(valid_types))}[/]")
        raise typer.Exit(1)
    lifecycle.send_message(store, task_id, msg_type, message)
    console.print(f"[green]Sent {msg_type} to {task_id}[/]")


@worker_app.command("list")
def worker_list() -> None:
    """List all workers."""
    orch = store.get_active_orchestrator()
    if not orch:
        console.print("[yellow]No active orchestrator.[/]")
        return

    lifecycle.refresh_worker_states(store, orch.id)
    sessions = store.list_sessions(orch.id)

    if not sessions:
        console.print("[dim]No workers.[/]")
        return

    table = Table(title="Workers")
    table.add_column("Task ID", style="cyan")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Skills")
    table.add_column("Idle", justify="right")

    for s in sessions:
        idle = ""
        if s.status == "running":
            idle = f"{int(s.idle_seconds)}s"
        table.add_row(s.task_id, s.role, s.status, s.skills, idle)

    console.print(table)


@worker_app.command("output")
def worker_output(
    task_id: str = typer.Argument(help="Task ID"),
    lines: int = typer.Option(30, "--lines", "-n", help="Number of lines to capture"),
) -> None:
    """Capture recent output from a worker."""
    output = lifecycle.check_worker_output(store, task_id, lines)
    if output:
        console.print(Panel(output, title=f"Worker {task_id} — last {lines} lines"))
    else:
        console.print(f"[yellow]No output for {task_id}[/]")


# ============================================================
# Crew commands (inter-agent communication)
# ============================================================

@crew_app.command("ask")
def crew_ask(
    task_id: str = typer.Argument(help="Your task ID"),
    question: str = typer.Argument(help="Question for the orchestrator"),
) -> None:
    """Ask the orchestrator a question (used by workers)."""
    session = store.get_session(task_id)
    if not session:
        console.print(f"[red]No session for {task_id}[/]")
        raise typer.Exit(1)

    # Store in SQLite
    store.put_message(task_id, MessageDirection.TO_ORCHESTRATOR, "normal", question)

    # Signal orchestrator via tmux
    orch = store.get_orchestrator(session.orchestrator_id)
    if orch:
        target = f"{orch.tmux_session}:orchestrator"
        try:
            tmux.send_command(target, f"[Worker {task_id} asks]: {question}")
        except Exception:
            pass

    console.print(f"[green]Question sent to orchestrator.[/]")


# ============================================================
# Hook commands (called by Claude Code hooks, not by users)
# ============================================================

@hook_app.command("session-start")
def hook_session_start_cmd(
    task_id: str = typer.Argument(help="Task ID"),
) -> None:
    """SessionStart hook — injects task context into worker agent."""
    output = handle_session_start(task_id)
    if output:
        # Print to stdout — Claude Code captures this and injects into context
        print(output)


@hook_app.command("heartbeat")
def hook_heartbeat_cmd(
    task_id: str = typer.Argument(help="Task ID"),
) -> None:
    """PostToolUse hook — lightweight heartbeat, just touches last_seen."""
    handle_heartbeat(task_id)


@hook_app.command("stop")
def hook_stop_cmd(
    task_id: str = typer.Argument(help="Task ID"),
) -> None:
    """Stop hook — checks beads status and signals orchestrator."""
    handle_stop(task_id)


# ============================================================
# Skill commands
# ============================================================

@skill_app.command("list")
def skill_list_cmd(
    repo: str = typer.Option("", "--repo", help="Skills repo path"),
) -> None:
    """List available skills."""
    repo_path = Path(repo) if repo else None
    catalog = skills.load_catalog(repo_path)

    if not catalog:
        console.print("[yellow]No skills found.[/]")
        console.print(f"[dim]Looking in: {skills.discover_skills_repo()}[/]")
        return

    table = Table(title="Available Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Extras", style="dim")

    for s in catalog:
        extras = []
        if s.has_scripts:
            extras.append("scripts")
        if s.has_references:
            extras.append("refs")
        table.add_row(s.name, s.description[:60], ", ".join(extras))

    console.print(table)


@skill_app.command("select")
def skill_select_cmd(
    description: str = typer.Argument(help="Task description"),
    role: str = typer.Option("explore", "--role", "-r"),
    max_skills: int = typer.Option(4, "--max", "-m"),
) -> None:
    """Use LLM to select skills for a task."""
    catalog = skills.load_catalog()
    if not catalog:
        console.print("[yellow]No skills available.[/]")
        return

    selected = skills.select_skills_via_llm(description, role, catalog, max_skills)

    if selected:
        console.print(f"[green]Selected skills:[/] {', '.join(selected)}")
    else:
        console.print("[yellow]No skills selected (or LLM call failed).[/]")


if __name__ == "__main__":
    app()
