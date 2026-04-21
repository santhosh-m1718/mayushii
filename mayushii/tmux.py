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
        timeout=10,
    )


def session_exists(session: str) -> bool:
    return _run(["has-session", "-t", session]).returncode == 0


def create_session(session: str, first_window: str = "orchestrator", cwd: str | None = None) -> None:
    if session_exists(session):
        return
    args = ["new-session", "-d", "-s", session, "-n", first_window, "-x", "200", "-y", "50"]
    if cwd:
        args += ["-c", cwd]
    _run(args, check=True)
    time.sleep(1)


def kill_session(session: str) -> None:
    if not session_exists(session):
        return
    _run(["kill-session", "-t", session])


def create_window(session: str, name: str, cwd: str | None = None, background: bool = True) -> str:
    """Create a new window, return the target string.

    background=True (default) keeps focus on the current window so the
    user stays on the orchestrator pane and workers run invisibly.
    """
    args = ["new-window"]
    if background:
        args.append("-d")
    args += ["-a", "-t", session, "-n", name]
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
    """Type text into a tmux pane. The primary communication channel.

    Raises RuntimeError if the target window doesn't exist or tmux rejects the command.
    """
    result = _run(["send-keys", "-t", target, "-l", text])
    if result.returncode != 0:
        raise RuntimeError(
            f"tmux send-keys failed for target '{target}': {result.stderr.strip()}"
        )
    if enter:
        result = _run(["send-keys", "-t", target, "Enter"])
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux send-keys Enter failed for target '{target}': {result.stderr.strip()}"
            )


def send_interrupt(target: str) -> None:
    """Send Ctrl-C to interrupt the current process."""
    result = _run(["send-keys", "-t", target, "C-c"])
    if result.returncode != 0:
        raise RuntimeError(
            f"tmux send-keys C-c failed for target '{target}': {result.stderr.strip()}"
        )


def capture_pane(target: str, lines: int = 50) -> str:
    """Capture visible pane content — useful for checking agent state."""
    result = _run(["capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
    return result.stdout.rstrip()


def wait_for_ready(target: str, sentinel: str = "❯", timeout: int = 30) -> bool:
    """Poll pane output until sentinel appears or timeout.

    Clears scrollback first to avoid matching stale content from before
    the command was launched.
    """
    _run(["send-keys", "-t", target, "clear", "Enter"])
    time.sleep(0.5)
    _run(["clear-history", "-t", target])
    deadline = time.time() + timeout
    while time.time() < deadline:
        output = capture_pane(target, lines=10)
        if sentinel in output:
            return True
        time.sleep(1)
    return False


def verify_target(target: str) -> None:
    """Check that a tmux target (session:window) exists. Raises RuntimeError if not."""
    parts = target.split(":", 1)
    sess = parts[0]
    if not session_exists(sess):
        raise RuntimeError(f"tmux session '{sess}' does not exist")
    if len(parts) > 1:
        window = parts[1]
        windows = {w.name for w in list_windows(sess)}
        if window not in windows:
            raise RuntimeError(
                f"tmux window '{window}' not found in session '{sess}' "
                f"(available: {', '.join(sorted(windows)) or 'none'})"
            )


def send_command(target: str, text: str) -> None:
    """Send a command (text + Enter). Verifies target exists first."""
    verify_target(target)
    send_keys(target, text, enter=True)
