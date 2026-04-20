"""Worker lifecycle management — start, stop, send messages, check status.

This is the core engine. It:
1. Creates workspaces with skills + hooks + CLAUDE.md
2. Spawns Claude Code in tmux windows
3. Sends the 4 message types (nudge/status/normal/divert)
4. Monitors worker state
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from orchestra import tmux
from orchestra.store import Store, Session, MessageDirection
from orchestra.skills import inject_skills, discover_skills_repo, load_catalog
from orchestra.hooks import write_workspace_settings, write_workspace_claude_md


ORCHESTRA_HOME = Path.home() / ".orchestra"
WORKSPACES_DIR = ORCHESTRA_HOME / "workspaces"


def _get_repo_path() -> Path:
    """Get the orchestra repo path from stored config."""
    default_repo_file = ORCHESTRA_HOME / "default-repo"
    if default_repo_file.exists():
        return Path(default_repo_file.read_text().strip())
    return Path(__file__).parent.parent


def _get_roles_dir() -> Path:
    return _get_repo_path() / "roles"

MAX_TMUX_MESSAGE_LEN = 4096


def _sanitize_window_name(name: str) -> str:
    """Strip characters that break tmux window names (dots, colons, etc.)."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
    return safe[:60]


def _load_role_prompt(role: str) -> str:
    """Load a role prompt template from roles/<role>.md"""
    role_file = _get_roles_dir() / f"{role}.md"
    if role_file.exists():
        return role_file.read_text()
    return f"You are a {role} agent. Complete your assigned task thoroughly."


def create_workspace(task_id: str) -> Path:
    """Create an isolated workspace directory for a worker."""
    workspace = WORKSPACES_DIR / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


DEFAULT_MODEL = "claude-opus-4-6"


def start_worker(
    store: Store,
    orchestrator_id: str,
    orch_session: str,
    task_id: str,
    role: str,
    skills: list[str],
    context: str = "",
    prompt: str | None = None,
    repo_path: str | None = None,
    model: str = DEFAULT_MODEL,
) -> Session:
    """Launch a worker agent in a tmux window.

    Steps:
    1. Create workspace directory
    2. Inject skills as symlinks
    3. Write CLAUDE.md with role + task context
    4. Install hooks (completion signal, edit tracking)
    5. Record session in SQLite BEFORE launching
    6. Create tmux window
    7. Launch Claude Code
    8. Wait for ready, then send initial prompt
    """
    # Workspace setup — if repo_path given, work there instead
    if repo_path:
        workspace = Path(repo_path)
    else:
        workspace = create_workspace(task_id)

    # Inject skills
    skills_repo = discover_skills_repo()
    if skills:
        inject_skills(workspace, skills, skills_repo)

    # Write CLAUDE.md
    role_prompt = _load_role_prompt(role)
    write_workspace_claude_md(workspace, role, task_id, role_prompt, context)

    # Install hooks (call back into orchestra CLI, not inline bash)
    write_workspace_settings(workspace, task_id)

    # Window name: role-taskid (sanitized for tmux safety)
    window_name = _sanitize_window_name(f"{role}-{task_id}")

    # Record in SQLite BEFORE launching (so hooks can find the session)
    session = store.put_session(
        task_id=task_id,
        orchestrator_id=orchestrator_id,
        tmux_session=orch_session,
        window_name=window_name,
        role=role,
        skills=",".join(skills),
        status="starting",
    )

    # Create tmux window in the orchestrator's session
    target = tmux.create_window(orch_session, window_name, cwd=str(workspace))

    # Wait for shell to be ready in the new tmux window
    tmux.wait_for_ready(target, sentinel="$", timeout=5)
    tmux.wait_for_ready(target, sentinel="%", timeout=3)

    # Launch Claude Code with model
    tmux.send_command(target, f"claude --model {model} --dangerously-skip-permissions")

    # Wait for Claude Code to be ready instead of fixed sleep
    ready = tmux.wait_for_ready(target, timeout=30)

    # Update status
    store.update_session_status(task_id, "running")

    # Send initial prompt
    if not prompt:
        prompt = f"Read your task: `bd show {task_id}`. Begin working."
    tmux.send_command(target, prompt)

    return session


def stop_worker(store: Store, task_id: str, cleanup: bool = True) -> None:
    """Gracefully stop a worker — Ctrl-C, wait, then kill window."""
    session = store.get_session(task_id)
    if not session:
        return

    target = session.tmux_target

    # Only interact with tmux if the session still exists
    if tmux.session_exists(session.tmux_session):
        windows = {w.name for w in tmux.list_windows(session.tmux_session)}
        if session.window_name in windows:
            tmux.send_interrupt(target)
            import time
            time.sleep(3)
            tmux.kill_window(session.tmux_session, session.window_name)

    # Update state
    store.update_session_status(task_id, "stopped")

    # Clean up workspace (only if it's our managed workspace, not a user repo)
    if cleanup:
        workspace = WORKSPACES_DIR / task_id
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)


def send_message(
    store: Store,
    task_id: str,
    msg_type: str,
    content: str,
) -> None:
    """Send a message to a worker using one of the 4 message types.

    - nudge: lightweight context injection (send-keys, no interrupt)
    - status: /btw query (doesn't pollute main context)
    - normal: full-context message
    - divert: interrupt + redirect (Ctrl-C then new message)
    """
    session = store.get_session(task_id)
    if not session:
        raise ValueError(f"No active session for task {task_id}")

    target = session.tmux_target

    # Truncate oversized messages that could overflow tmux
    if len(content) > MAX_TMUX_MESSAGE_LEN:
        content = content[:MAX_TMUX_MESSAGE_LEN] + "\n... [truncated]"

    # Record the message
    store.put_message(task_id, MessageDirection.TO_WORKER, msg_type, content)

    if msg_type == "nudge":
        tmux.send_command(target, content)

    elif msg_type == "status":
        tmux.send_command(target, f"/btw {content}")

    elif msg_type == "divert":
        tmux.send_interrupt(target)
        import time
        time.sleep(2)
        tmux.send_command(target, content)

    elif msg_type == "normal":
        tmux.send_command(target, content)

    else:
        raise ValueError(f"Unknown message type: {msg_type}")


def check_worker_output(store: Store, task_id: str, lines: int = 30) -> str:
    """Capture recent output from a worker's tmux pane."""
    session = store.get_session(task_id)
    if not session:
        return ""
    return tmux.capture_pane(session.tmux_target, lines)


def cleanup_workspace(task_id: str) -> None:
    """Remove a worker's workspace directory."""
    workspace = WORKSPACES_DIR / task_id
    if workspace.exists():
        shutil.rmtree(workspace)


def list_workers(store: Store, orchestrator_id: str) -> list[Session]:
    """List all workers for an orchestrator."""
    return store.list_sessions(orchestrator_id)


def refresh_worker_states(store: Store, orchestrator_id: str) -> None:
    """Sync worker states with actual tmux window state.

    If a tmux window is gone but the session is still 'running',
    check beads to determine if it completed or failed.
    """
    sessions = store.list_running_sessions(orchestrator_id)
    if not sessions:
        return

    orch = store.get_orchestrator(orchestrator_id)
    if not orch:
        return

    if not tmux.session_exists(orch.tmux_session):
        for session in sessions:
            store.update_session_status(session.task_id, "stopped")
        return

    windows = {w.name for w in tmux.list_windows(orch.tmux_session)}

    for session in sessions:
        if session.window_name not in windows:
            # Check beads to see if the task was properly closed
            import subprocess
            from orchestra.hooks import _beads_env
            try:
                result = subprocess.run(
                    ["bd", "show", session.task_id, "--json"],
                    capture_output=True, text=True, check=False,
                    env=_beads_env(),
                )
                if result.returncode == 0:
                    import json
                    data = json.loads(result.stdout)
                    if isinstance(data, list):
                        data = data[0]
                    if data.get("status") == "closed":
                        store.update_session_status(session.task_id, "done")
                        continue
            except Exception:
                pass
            # Window gone but task not closed = unexpected exit
            store.update_session_status(session.task_id, "failed")
