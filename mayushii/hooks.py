"""Hook generation — creates Claude Code hooks that call back into mayushii CLI.

Three hooks per worker workspace:
- SessionStart: injects task context when Claude Code boots
- PostToolUse: heartbeat (touch last_seen in SQLite)
- Stop: signals orchestrator that worker finished/failed

Hooks are written to .claude/settings.json so Claude Code loads them automatically.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mayushii.store import Store


_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _validate_task_id(task_id: str) -> str:
    """Validate task_id before embedding in shell commands or file paths."""
    if not _TASK_ID_RE.match(task_id) or ".." in task_id:
        raise ValueError(f"Invalid task_id for hook: '{task_id}'")
    return task_id


def _get_repo_path() -> Path:
    """Get the repo path from stored config."""
    default_repo_file = Path.home() / ".mayushii" / "default-repo"
    if default_repo_file.exists():
        return Path(default_repo_file.read_text().strip())
    return Path(__file__).parent.parent


def _beads_env() -> dict[str, str]:
    """Return env dict with BEADS_DIR set so bd commands find the database."""
    env = os.environ.copy()
    repo = _get_repo_path()
    beads_dir = repo / ".beads"
    if beads_dir.exists():
        env["BEADS_DIR"] = str(beads_dir)
    return env


def generate_hooks_config(task_id: str) -> dict:
    """Generate .claude/settings.json hooks that call back into mayushii CLI."""
    _validate_task_id(task_id)
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": f"mayushii hook session-start {task_id}",
                    }],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": f"mayushii hook heartbeat {task_id}",
                    }],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": f"mayushii hook stop {task_id}",
                    }],
                }
            ],
        }
    }


def write_workspace_settings(workspace: Path, task_id: str) -> Path:
    """Write .claude/settings.json with hooks into the workspace."""
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    hooks_config = generate_hooks_config(task_id)

    existing = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            pass

    existing.update(hooks_config)
    settings_path.write_text(json.dumps(existing, indent=2))
    return settings_path


def generate_claude_md(role: str, task_id: str, role_prompt: str, context: str = "") -> str:
    """Generate CLAUDE.md content for a worker workspace."""
    sections = [
        f"# Worker Agent — {role.title()}",
        "",
        "## CRITICAL: You MUST close your task when done",
        f"When you finish your work, you MUST run: `bd close {task_id} --reason \"<summary of what you found/did>\"`",
        "This is your #1 obligation. Do NOT stop, go idle, or wait after finishing — close the task immediately.",
        "If you don't close the task, the orchestrator will never know you finished.",
        "",
        "## Your Task",
        f"Run `bd show {task_id}` to see your full task details.",
        "",
        "## Role",
        role_prompt,
    ]

    if context:
        sections.extend([
            "",
            "## Context from Prior Tasks",
            context,
        ])

    sections.extend([
        "",
        "## If Blocked",
        f"`bd update {task_id} --status blocked --append-notes \"BLOCKED: <describe the issue>\"`",
        "",
        "## To Ask the Orchestrator a Question",
        f"`mayushii crew ask {task_id} \"your question here\"`",
        "The orchestrator will see your question and can send you a response.",
    ])

    return "\n".join(sections)


def write_workspace_claude_md(
    workspace: Path,
    role: str,
    task_id: str,
    role_prompt: str,
    context: str = "",
) -> Path:
    """Write CLAUDE.md into the workspace root."""
    claude_md = workspace / "CLAUDE.md"
    content = generate_claude_md(role, task_id, role_prompt, context)
    claude_md.write_text(content)
    return claude_md


PROMPTS_DIR = Path.home() / ".mayushii" / "prompts"


def write_worker_prompt(
    task_id: str,
    role: str,
    role_prompt: str,
    context: str = "",
) -> Path:
    """Write worker prompt to ~/.mayushii/prompts/<task-id>.md instead of repo's CLAUDE.md."""
    _validate_task_id(task_id)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPTS_DIR / f"{task_id}.md"
    if not prompt_path.resolve().is_relative_to(PROMPTS_DIR.resolve()):
        raise ValueError(f"Task ID '{task_id}' would escape prompts directory")
    content = generate_claude_md(role, task_id, role_prompt, context)
    prompt_path.write_text(content)
    return prompt_path


def cleanup_worker_prompt(task_id: str) -> None:
    """Remove a worker's prompt file."""
    _validate_task_id(task_id)
    prompt_path = PROMPTS_DIR / f"{task_id}.md"
    if not prompt_path.resolve().is_relative_to(PROMPTS_DIR.resolve()):
        return
    if prompt_path.exists():
        prompt_path.unlink()


# --- Hook implementations (called by `mayushii hook` CLI commands) ---

def handle_session_start(task_id: str) -> str:
    """Called by SessionStart hook. Returns context to inject into agent."""
    lines: list[str] = []

    # Get task details from beads
    try:
        result = subprocess.run(
            ["bd", "show", task_id, "--json"],
            capture_output=True, text=True, check=False,
            env=_beads_env(),
        )
        if result.returncode == 0:
            task_data = json.loads(result.stdout)
            if isinstance(task_data, list):
                task_data = task_data[0]
            lines.append(f"## Your Task: {task_id}")
            lines.append(f"**Title:** {task_data.get('title', 'unknown')}")
            desc = task_data.get("description", "")
            if desc:
                lines.append(f"**Description:** {desc}")
            lines.append("")
    except Exception:
        lines.append(f"## Your Task: {task_id}")
        lines.append("(Could not fetch task details from beads — run `bd show` manually)")
        lines.append("")

    # Check for pending messages from orchestrator
    try:
        store = Store()
        messages = store.get_pending_messages(task_id, "to_worker")
        if messages:
            lines.append("## Messages from Orchestrator")
            for msg in messages:
                lines.append(f"- {msg.content}")
                store.ack_message(msg.id)
            lines.append("")

        store.update_session_status(task_id, "running")
    except Exception:
        pass

    return "\n".join(lines)


def handle_heartbeat(task_id: str) -> None:
    """Called by PostToolUse hook. Lightweight — just touches last_seen."""
    try:
        store = Store()
        store.touch_session(task_id)
    except Exception:
        pass


def handle_stop(task_id: str) -> None:
    """Called by Stop hook. Checks beads status and signals orchestrator."""
    store = Store()
    session = store.get_session(task_id)
    if not session:
        return

    # Check if task was closed in beads
    status = "done"
    reason = "session ended"
    try:
        result = subprocess.run(
            ["bd", "show", task_id, "--json"],
            capture_output=True, text=True, check=False,
            env=_beads_env(),
        )
        if result.returncode == 0:
            task_data = json.loads(result.stdout)
            if isinstance(task_data, list):
                task_data = task_data[0]
            if task_data.get("status") == "closed":
                reason = task_data.get("close_reason", "completed")
                status = "done"
            else:
                reason = "session ended without closing task"
                status = "failed"
    except Exception:
        reason = "session ended (could not check beads)"

    # Atomically transition status; skip if already terminal (prevents duplicate signals)
    if not store.try_terminal_transition(task_id, status):
        return

    # Signal orchestrator via tmux — critical path, don't silently swallow failures
    if session.orchestrator_id:
        orch = store.get_orchestrator(session.orchestrator_id)
        if orch:
            from mayushii import tmux
            target = f"{orch.tmux_session}:orchestrator"
            if tmux.session_exists(orch.tmux_session):
                windows = {w.name for w in tmux.list_windows(orch.tmux_session)}
                if "orchestrator" in windows:
                    tmux.send_command(target, f"[Worker {task_id}]: {status} — {reason}")
                else:
                    print(f"[mayushii] WARNING: orchestrator window gone, completion signal for {task_id} lost", file=sys.stderr)
            else:
                print(f"[mayushii] WARNING: tmux session {orch.tmux_session} gone, completion signal for {task_id} lost", file=sys.stderr)
