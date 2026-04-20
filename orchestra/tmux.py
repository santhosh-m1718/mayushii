"""Thin tmux wrapper — direct port of JEFF's tmux.go patterns.

All agent communication flows through tmux send-keys.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass


@dataclass
class Window:
    index: int
    name: str
    active: bool


def ensure_tmux() -> None:
    """Check that tmux is installed, raise a clear error if not."""
    if not shutil.which("tmux"):
        raise FileNotFoundError(
            "tmux not found. Install it with: brew install tmux"
        )


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    ensure_tmux()
    return subprocess.run(
        ["tmux"] + args,
        capture_output=True,
        text=True,
        check=check,
    )


def session_exists(session: str) -> bool:
    return _run(["has-session", "-t", session]).returncode == 0


def create_session(session: str, first_window: str = "orchestrator") -> None:
    if session_exists(session):
        return
    _run(["new-session", "-d", "-s", session, "-n", first_window], check=True)


def kill_session(session: str) -> None:
    if not session_exists(session):
        return
    _run(["kill-session", "-t", session])


def create_window(session: str, name: str, cwd: str | None = None) -> str:
    """Create a new window, return the target string."""
    args = ["new-window", "-a", "-t", session, "-n", name]
    if cwd:
        args += ["-c", cwd]
    _run(args, check=True)
    return f"{session}:{name}"


def kill_window(session: str, window: str) -> None:
    _run(["kill-window", "-t", f"{session}:{window}"])


def list_windows(session: str) -> list[Window]:
    if not session_exists(session):
        return []
    result = _run([
        "list-windows", "-t", session,
        "-F", "#{window_index}|#{window_name}|#{window_active}",
    ])
    if not result.stdout.strip():
        return []
    windows = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("|")
        if len(parts) == 3:
            windows.append(Window(
                index=int(parts[0]),
                name=parts[1],
                active=parts[2] == "1",
            ))
    return windows


def send_keys(target: str, text: str, enter: bool = True) -> None:
    """Type text into a tmux pane. The primary communication channel."""
    _run(["send-keys", "-t", target, "-l", text])
    if enter:
        _run(["send-keys", "-t", target, "Enter"])


def send_interrupt(target: str) -> None:
    """Send Ctrl-C to interrupt the current process."""
    _run(["send-keys", "-t", target, "C-c"])


def capture_pane(target: str, lines: int = 50) -> str:
    """Capture visible pane content — useful for checking agent state."""
    result = _run(["capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
    return result.stdout.rstrip()


def wait_for_ready(target: str, sentinel: str = "❯", timeout: int = 30) -> bool:
    """Poll pane output until sentinel appears or timeout.

    Better than sleep(3) — detects when Claude Code is actually ready.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        output = capture_pane(target, lines=5)
        if sentinel in output:
            return True
        time.sleep(1)
    return False


def send_command(target: str, text: str) -> None:
    """Send a command (text + Enter). Alias for clarity."""
    send_keys(target, text, enter=True)
