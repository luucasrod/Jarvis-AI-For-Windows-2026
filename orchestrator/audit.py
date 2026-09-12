"""Audit log for orchestrator actions (issue #20).

Every important action (create Issue, pause agent, send decision, merge)
leaves a traceable record: timestamp, action, project, origin, result,
correlation id. Secrets are redacted defense-in-depth (extra dict keys
that look like credentials are never written, even if a caller forgets
to scrub them).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from orchestrator.persistence import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    project_id TEXT,
    origin TEXT NOT NULL,
    result TEXT NOT NULL,
    correlation_id TEXT,
    extra TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_SECRET_KEY_MARKERS = ("token", "password", "senha", "key", "secret", "credential")


def _ensure_table(store: Store) -> None:
    store.ensure_schema(_SCHEMA)


def _redact_value(value):
    """Recurses into dicts/lists so a secret buried at any depth (a
    nested request body, a list of attempt records, ...) is redacted
    just like a top-level one - the original object is never mutated
    (Review Task #103)."""
    if isinstance(value, dict):
        return _redact_secrets(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _redact_secrets(extra: dict) -> dict:
    """Redacts any key whose name suggests it holds a credential,
    regardless of its value's type or nesting depth - defense in depth,
    never trust the caller to have already scrubbed `extra`."""
    redacted = {}
    for key, value in extra.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            redacted[key] = "***"
        else:
            redacted[key] = _redact_value(value)
    return redacted


def record(
    store: Store,
    action: str,
    origin: str,
    result: str,
    project_id: str | None = None,
    correlation_id: str | None = None,
    extra: dict | None = None,
) -> None:
    _ensure_table(store)
    safe_extra = _redact_secrets(extra or {})
    store.execute(
        "INSERT INTO audit_log (action, project_id, origin, result, correlation_id, extra, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            action,
            project_id,
            origin,
            result,
            correlation_id,
            json.dumps(safe_extra),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def query_audit(
    store: Store,
    project_id: str | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """Basic filtered query - no dashboard/rich interface (out of scope,
    per the issue)."""
    _ensure_table(store)

    clauses: list[str] = []
    params: list = []

    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if since is not None:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        clauses.append("created_at >= ?")
        params.append(since_utc.isoformat())

    sql = "SELECT action, project_id, origin, result, correlation_id, extra, created_at FROM audit_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC, id ASC"

    rows = store.query(sql, tuple(params))
    return [
        {
            "action": r[0],
            "project_id": r[1],
            "origin": r[2],
            "result": r[3],
            "correlation_id": r[4],
            "extra": json.loads(r[5]),
            "created_at": datetime.fromisoformat(r[6]),
        }
        for r in rows
    ]
