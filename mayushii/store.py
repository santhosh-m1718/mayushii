"""SQLite store for crew state — orchestrators, sessions (workers), messages.

Beads owns the task DAG. This store owns the crew runtime state.
Mirrors JEFF's proven schema adapted for Python.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Generator

from . import tmux as _tmux


MAYUSHII_HOME = Path.home() / ".mayushii"
DB_PATH = MAYUSHII_HOME / "mayushii.db"


class SessionStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


class MessageType(str, Enum):
    NUDGE = "nudge"
    STATUS = "status"
    NORMAL = "normal"
    DIVERT = "divert"


class MessageDirection(str, Enum):
    TO_WORKER = "to_worker"
    TO_ORCHESTRATOR = "to_orchestrator"


@dataclass
class Orchestrator:
    id: str
    tmux_session: str
    status: str
    started_at: float


@dataclass
class Session:
    task_id: str
    tmux_session: str
    window_name: str
    role: str
    skills: str
    orchestrator_id: str
    status: str
    started_at: float
    stopped_at: float | None = None
    last_seen: float | None = None

    @property
    def tmux_target(self) -> str:
        return f"{self.tmux_session}:{self.window_name}"

    @property
    def idle_seconds(self) -> float:
        if self.last_seen is None:
            return time.time() - self.started_at
        return time.time() - self.last_seen


@dataclass
class Message:
    id: str
    task_id: str
    direction: str
    msg_type: str
    content: str
    response: str | None
    created_at: float
    acked_at: float | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS orchestrators (
    id TEXT PRIMARY KEY,
    tmux_session TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    task_id TEXT PRIMARY KEY,
    tmux_session TEXT NOT NULL,
    window_name TEXT NOT NULL,
    role TEXT NOT NULL,
    skills TEXT NOT NULL DEFAULT '',
    orchestrator_id TEXT NOT NULL REFERENCES orchestrators(id),
    status TEXT NOT NULL DEFAULT 'starting',
    started_at REAL NOT NULL,
    stopped_at REAL,
    last_seen REAL,
    UNIQUE(tmux_session, window_name)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES sessions(task_id),
    direction TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    content TEXT NOT NULL,
    response TEXT,
    created_at REAL NOT NULL,
    acked_at REAL
);

CREATE INDEX IF NOT EXISTS idx_sessions_orchestrator ON sessions(orchestrator_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_unacked ON messages(acked_at) WHERE acked_at IS NULL;
"""

MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN last_seen REAL",
]


class Store:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- Orchestrators ---

    def create_orchestrator(self, tmux_session: str) -> Orchestrator:
        orch = Orchestrator(
            id=_new_id(),
            tmux_session=tmux_session,
            status="running",
            started_at=time.time(),
        )
        with self._conn() as conn:
            # Cascade: clean up all sessions and messages tied to the old orchestrator
            old = conn.execute(
                "SELECT id FROM orchestrators WHERE tmux_session = ?", (tmux_session,)
            ).fetchone()
            if old:
                # Kill orphaned tmux windows before removing from DB
                old_sessions = conn.execute(
                    "SELECT tmux_session, window_name FROM sessions WHERE orchestrator_id = ? AND status IN ('starting', 'running')",
                    (old["id"],),
                ).fetchall()
                for s in old_sessions:
                    try:
                        _tmux.kill_window(s["tmux_session"], s["window_name"])
                    except Exception:
                        pass
                # Stop any active sessions
                conn.execute(
                    "UPDATE sessions SET status = 'stopped', stopped_at = ? WHERE orchestrator_id = ? AND status IN ('starting', 'running')",
                    (time.time(), old["id"]),
                )
                # Delete messages for old sessions, then delete sessions, then orchestrator
                conn.execute(
                    "DELETE FROM messages WHERE task_id IN (SELECT task_id FROM sessions WHERE orchestrator_id = ?)",
                    (old["id"],),
                )
                conn.execute("DELETE FROM sessions WHERE orchestrator_id = ?", (old["id"],))
                conn.execute("DELETE FROM orchestrators WHERE id = ?", (old["id"],))
            conn.execute(
                "INSERT INTO orchestrators (id, tmux_session, status, started_at) VALUES (?, ?, ?, ?)",
                (orch.id, orch.tmux_session, orch.status, orch.started_at),
            )
        return orch

    def get_orchestrator(self, orch_id: str) -> Orchestrator | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM orchestrators WHERE id = ?", (orch_id,)).fetchone()
        if not row:
            return None
        return Orchestrator(**dict(row))

    def get_active_orchestrator(self) -> Orchestrator | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM orchestrators WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return Orchestrator(**dict(row))

    def stop_orchestrator(self, orch_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE orchestrators SET status = 'stopped' WHERE id = ?", (orch_id,))

    # --- Sessions (workers) ---

    def put_session(
        self,
        task_id: str,
        orchestrator_id: str,
        tmux_session: str,
        window_name: str,
        role: str,
        skills: str,
        status: str = "starting",
    ) -> Session:
        now = time.time()
        session = Session(
            task_id=task_id,
            tmux_session=tmux_session,
            window_name=window_name,
            role=role,
            skills=skills,
            orchestrator_id=orchestrator_id,
            status=status,
            started_at=now,
            last_seen=now,
        )
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT status FROM sessions WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing and existing["status"] in ("starting", "running"):
                raise ValueError(
                    f"Session {task_id} already active (status={existing['status']}). "
                    "Stop it first with `mayushii worker stop`."
                )
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (task_id, tmux_session, window_name, role, skills, orchestrator_id, status, started_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session.task_id, session.tmux_session, session.window_name,
                 session.role, session.skills, session.orchestrator_id,
                 session.status, session.started_at, session.last_seen),
            )
        return session

    def get_session(self, task_id: str) -> Session | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return Session(**dict(row))

    def update_session_status(self, task_id: str, status: str) -> None:
        with self._conn() as conn:
            if status in ("done", "failed", "stopped"):
                conn.execute(
                    "UPDATE sessions SET status = ?, stopped_at = ? WHERE task_id = ?",
                    (status, time.time(), task_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET status = ? WHERE task_id = ?",
                    (status, task_id),
                )

    def touch_session(self, task_id: str) -> None:
        """Update last_seen timestamp — called by PostToolUse hook as heartbeat."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen = ? WHERE task_id = ?",
                (time.time(), task_id),
            )

    def list_sessions(self, orchestrator_id: str, status: str | None = None) -> list[Session]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE orchestrator_id = ? AND status = ? ORDER BY started_at",
                    (orchestrator_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE orchestrator_id = ? ORDER BY started_at",
                    (orchestrator_id,),
                ).fetchall()
        return [Session(**dict(r)) for r in rows]

    def list_running_sessions(self, orchestrator_id: str) -> list[Session]:
        return self.list_sessions(orchestrator_id, status="running")

    def list_stale_sessions(self, orchestrator_id: str, threshold_minutes: int = 10) -> list[Session]:
        """Find running workers with no heartbeat for longer than threshold."""
        cutoff = time.time() - (threshold_minutes * 60)
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE orchestrator_id = ? AND status = 'running'
                   AND (last_seen IS NULL OR last_seen < ?)
                   ORDER BY last_seen""",
                (orchestrator_id, cutoff),
            ).fetchall()
        return [Session(**dict(r)) for r in rows]

    # --- Messages ---

    def put_message(
        self,
        task_id: str,
        direction: str,
        msg_type: str,
        content: str,
    ) -> Message:
        msg = Message(
            id=_new_id(),
            task_id=task_id,
            direction=direction,
            msg_type=msg_type,
            content=content,
            response=None,
            created_at=time.time(),
        )
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO messages (id, task_id, direction, msg_type, content, response, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (msg.id, msg.task_id, msg.direction, msg.msg_type, msg.content, msg.response, msg.created_at),
            )
        return msg

    def get_pending_messages(self, task_id: str, direction: str) -> list[Message]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE task_id = ? AND direction = ? AND acked_at IS NULL ORDER BY created_at",
                (task_id, direction),
            ).fetchall()
        return [Message(**dict(r)) for r in rows]

    def ack_message(self, msg_id: str, response: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET acked_at = ?, response = ? WHERE id = ?",
                (time.time(), response, msg_id),
            )


def _new_id() -> str:
    return uuid.uuid4().hex[:12]
