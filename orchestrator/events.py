"""Append-only internal event log (issue #14).

Used by the history module (#28) and for debugging/observability. No pub/
sub, no reactive handlers here (out of scope - see #14) - just emit() and
query_events(). Shares the same SQLite file as orchestrator.persistence
via the generic Store.ensure_schema/execute/query helpers added for this.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum

from orchestrator.persistence import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    correlation_id TEXT,
    project_id TEXT,
    created_at TEXT NOT NULL
);
"""


class EventType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_READY = "task_ready"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_FAILED = "review_failed"
    REVIEW_PASSED = "review_passed"
    AGENT_RATE_LIMITED = "agent_rate_limited"
    AGENT_AVAILABLE = "agent_available"
    BUG_FOUND = "bug_found"
    DEPLOYMENT_STARTED = "deployment_started"
    DEPLOYMENT_FINISHED = "deployment_finished"
    DECISION_REQUIRED = "decision_required"
    DECISION_RECEIVED = "decision_received"
    # Added in #19 (Telegram foundation) - not in the original section 40
    # list but required by #19's own scope ("mensagem recebida no canal
    # de controle vira um evento telegram_message_received").
    TELEGRAM_MESSAGE_RECEIVED = "telegram_message_received"


def _ensure_table(store: Store) -> None:
    store.ensure_schema(_SCHEMA)


def emit_in_transaction(
    connection: sqlite3.Connection,
    event_type: EventType,
    payload: dict | None = None,
    correlation_id: str | None = None,
    project_id: str | None = None,
    *,
    created_at: datetime | None = None,
) -> None:
    """Append inside a caller-owned transaction without committing it (#25).

    This lets a scheduler event and its once-per-day guard survive together.
    Uses execute (one DDL statement), never executescript's implicit commit.
    """
    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    connection.execute(_SCHEMA)
    connection.execute(
        "INSERT INTO events (event_type, payload, correlation_id, project_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type.value, json.dumps(payload or {}), correlation_id, project_id,
         timestamp.astimezone(timezone.utc).isoformat()),
    )


def emit(
    store: Store,
    event_type: EventType,
    payload: dict | None = None,
    correlation_id: str | None = None,
    project_id: str | None = None,
) -> None:
    _ensure_table(store)
    store.execute(
        "INSERT INTO events (event_type, payload, correlation_id, project_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event_type.value,
            json.dumps(payload or {}),
            correlation_id,
            project_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def query_events(
    store: Store,
    since: datetime | None = None,
    event_types: list[EventType] | None = None,
    project_id: str | None = None,
    correlation_id: str | None = None,
) -> list[dict]:
    _ensure_table(store)

    clauses: list[str] = []
    params: list = []

    if since is not None:
        # `created_at` is always stored in UTC (see emit()). Comparing
        # ISO-8601 strings lexicographically only works when both sides
        # use the SAME UTC offset representation - a naive `since.isoformat()`
        # compared a value with e.g. "+01:00" (Europe/Lisbon in summer)
        # against stored "+00:00" values and silently returned the wrong
        # rows (found in review #56). Normalize `since` to UTC first;
        # a naive datetime (no tzinfo) is assumed to already be UTC.
        since_utc = since.astimezone(timezone.utc) if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        clauses.append("created_at >= ?")
        params.append(since_utc.isoformat())
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        clauses.append(f"event_type IN ({placeholders})")
        params.extend(t.value for t in event_types)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if correlation_id is not None:
        clauses.append("correlation_id = ?")
        params.append(correlation_id)

    sql = "SELECT event_type, payload, correlation_id, project_id, created_at FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC, id ASC"

    rows = store.query(sql, tuple(params))
    return [
        {
            "event_type": EventType(r[0]),
            "payload": json.loads(r[1]),
            "correlation_id": r[2],
            "project_id": r[3],
            "created_at": datetime.fromisoformat(r[4]),
        }
        for r in rows
    ]
