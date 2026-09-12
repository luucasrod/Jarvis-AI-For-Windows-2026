"""SQLite-backed persistence for the orchestrator (issue #13).

Survives Jarvis/Paperclip/PC restarts (PROMPT MESTRE V2 section 38) without
any external service - stdlib sqlite3, no ORM. One `Store` per process,
safe to share across threads (the voice loop and the future scheduler
thread both touch this) via a re-entrant lock guarding every operation.

Out of scope here: the detailed event log (issue #14 - separate table,
same file) and any business logic about *when* to rate-limit an agent
(issue #19 - this module only stores/retrieves that state).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from orchestrator.models import Task, TaskState

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "orchestrator_state.db"
_T = TypeVar("_T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    correlation_id TEXT PRIMARY KEY,
    task_id TEXT,
    message TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    response TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS rate_limits (
    agent TEXT PRIMARY KEY,
    reason TEXT,
    reset_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    correlation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (correlation_id, kind)
);
"""


class Store:
    """One instance per process is the intended usage. Thread-safe: every
    public method acquires `self._lock` for the duration of its SQLite
    call. `check_same_thread=False` is required because the voice thread
    and a future scheduler thread both use the same Store instance."""

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH):
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- generic helpers for other modules sharing this connection -----
    # Added in #14 so events.py (and future modules) can add their own
    # tables/queries without reaching into Store's private attributes.

    def ensure_schema(self, schema_sql: str) -> None:
        with self._lock:
            self._conn.executescript(schema_sql)
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- tasks -------------------------------------------------------

    def save_task(self, task: Task) -> None:
        data = json.dumps(task.to_dict())
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, state, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, data=excluded.data",
                (task.id, task.state.value, data),
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return Task.from_dict(json.loads(row[0]))

    def list_tasks(self, state: TaskState | None = None) -> list[Task]:
        with self._lock:
            if state is None:
                rows = self._conn.execute("SELECT data FROM tasks").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM tasks WHERE state = ?", (state.value,)
                ).fetchall()
        return [Task.from_dict(json.loads(r[0])) for r in rows]

    # --- decisions (NEEDS_LUCAS) --------------------------------------

    def save_decision(
        self,
        correlation_id: str,
        message: str,
        task_id: str | None = None,
        created_at: str | None = None,
    ) -> None:
        from datetime import datetime, timezone

        created_at = created_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions (correlation_id, task_id, message, resolved, created_at) "
                "VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(correlation_id) DO UPDATE SET message=excluded.message, task_id=excluded.task_id",
                (correlation_id, task_id, message, created_at),
            )
            self._conn.commit()

    def get_pending_decisions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT correlation_id, task_id, message, created_at FROM decisions WHERE resolved = 0"
            ).fetchall()
        return [
            {"correlation_id": r[0], "task_id": r[1], "message": r[2], "created_at": r[3]}
            for r in rows
        ]

    def resolve_decision(self, correlation_id: str, response: str) -> None:
        from datetime import datetime, timezone

        with self._lock:
            self._conn.execute(
                "UPDATE decisions SET resolved = 1, response = ?, resolved_at = ? WHERE correlation_id = ?",
                (response, datetime.now(timezone.utc).isoformat(), correlation_id),
            )
            self._conn.commit()

    # --- rate limits ---------------------------------------------------

    def set_rate_limit(self, agent: str, reason: str, reset_at: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_limits (agent, reason, reset_at) VALUES (?, ?, ?) "
                "ON CONFLICT(agent) DO UPDATE SET reason=excluded.reason, reset_at=excluded.reset_at",
                (agent, reason, reset_at),
            )
            self._conn.commit()

    def get_rate_limit(self, agent: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT reason, reset_at FROM rate_limits WHERE agent = ?", (agent,)
            ).fetchone()
        if row is None:
            return None
        return {"agent": agent, "reason": row[0], "reset_at": row[1]}

    def clear_rate_limit(self, agent: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM rate_limits WHERE agent = ?", (agent,))
            self._conn.commit()

    # --- sync state ------------------------------------------------------

    def run_in_transaction(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run local SQL effects atomically, including across Store instances.

        The callback uses only the supplied connection; it must not commit,
        call other Store methods, use executescript, or perform network I/O.
        Existing Store methods retain their commit-per-call behavior.
        """
        with self._lock:
            if self._conn.in_transaction:
                raise RuntimeError("Nested Store transactions are not supported")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = operation(self._conn)
                self._conn.commit()
                return result
            except BaseException:
                self._conn.rollback()
                raise

    def run_sync_once(
        self, key: str, value: str, operation: Callable[[sqlite3.Connection], None]
    ) -> bool:
        """Commit an action and its sync guard together, or neither on failure.

        Use a distinct key per occurrence (scheduler includes the local date).
        Callback restrictions are the same as run_in_transaction.
        """
        def apply(connection: sqlite3.Connection) -> bool:
            if connection.execute("SELECT 1 FROM sync_state WHERE key = ?", (key,)).fetchone():
                return False
            operation(connection)
            connection.execute("INSERT INTO sync_state (key, value) VALUES (?, ?)", (key, value))
            return True

        return self.run_in_transaction(apply)

    def set_sync_value(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_sync_value(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    # --- idempotency -------------------------------------------------

    def record_idempotency_key(self, correlation_id: str, kind: str) -> None:
        from datetime import datetime, timezone

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys (correlation_id, kind, created_at) VALUES (?, ?, ?)",
                (correlation_id, kind, datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def has_idempotency_key(self, correlation_id: str, kind: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM idempotency_keys WHERE correlation_id = ? AND kind = ?",
                (correlation_id, kind),
            ).fetchone()
        return row is not None
