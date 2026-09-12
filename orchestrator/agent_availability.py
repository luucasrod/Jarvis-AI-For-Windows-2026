"""Rate-limit detection, cooldown and FLEX task redirection (issue #26).

Separate from #24's agent_policy: that module produces the INITIAL
classification of a task; this module reacts when an agent already
assigned to work becomes temporarily unavailable. Detection of the real
rate-limit signal from each agent's own API/CLI is explicitly out of
scope (it depends on how Paperclip/the agent CLI eventually exposes that
error) - this module is the interface/reaction layer: record the
cooldown, answer availability queries, and redirect only what's safe to
redirect (READY FLEX tasks, never IN_PROGRESS work).

Fixed per Codex's review (Review Task #109, 3 P1 findings):
- The read (list READY tasks) and write (redirect) used to be two
  separate Store calls, leaving a window where another connection could
  start one of those tasks (READY -> IN_PROGRESS) in between - the
  redirect would then silently overwrite that IN_PROGRESS record back
  towards a stale READY snapshot. The whole read+redirect now runs inside
  one `run_in_transaction` (BEGIN IMMEDIATE): a concurrent writer on
  another connection is blocked until this transaction commits, and the
  SELECT inside it is always the latest committed state.
- Redirecting to `other` no longer assumes `other` is actually free: if
  it's ALSO currently rate-limited, the task's assignment is left alone
  (it wait for whichever agent recovers first) instead of moving it to
  an equally-unavailable destination.
- Flipping `preferred_agent` to `other` no longer leaves
  `reviewer_preference` pointing at that same agent - #24 requires a
  reviewer distinct from the implementer, and this module is the one
  responsible for keeping that true across a reassignment (its own
  docstring said so already). When they'd collide, the reviewer becomes
  the agent that was just rate-limited - it's not the one about to
  implement anymore, so it's free to review.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit, emit_in_transaction
from orchestrator.models import AgentClass, AgentName, Task, TaskState
from orchestrator.persistence import Store

_DefaultClock = Callable[[], datetime]


def _opposite_agent(agent: AgentName) -> AgentName:
    return AgentName.CODEX if agent == AgentName.CLAUDE else AgentName.CLAUDE


def _now(clock: _DefaultClock | None) -> datetime:
    instant = (clock or (lambda: datetime.now(timezone.utc)))()
    if instant.utcoffset() is None:
        raise ValueError("agent_availability clock must return a timezone-aware datetime")
    return instant.astimezone(timezone.utc)


def _available_in_connection(connection, agent: AgentName, now: datetime) -> bool:
    """Same rule as is_agent_available(), read from the connection already
    inside our transaction rather than via a separate Store call (see
    run_in_transaction's own restriction against calling other Store
    methods from within its callback)."""
    row = connection.execute(
        "SELECT reset_at FROM rate_limits WHERE agent = ?", (agent.value,)
    ).fetchone()
    if row is None:
        return True
    reset_at = row[0]
    if reset_at is None:
        return False
    return now >= datetime.fromisoformat(reset_at)


def mark_rate_limited(
    store: Store,
    agent: AgentName,
    reason: str,
    reset_at: datetime | None = None,
    *,
    config: OrchestratorConfig | None = None,
    clock: _DefaultClock | None = None,
) -> None:
    """Records `agent` as rate-limited and redirects its unstarted FLEX work.

    Without an explicit `reset_at`, applies the configurable default
    cooldown (`OrchestratorConfig.rate_limit_backoff_minutes`). Only
    FLEX-class tasks still in READY (never started, re-verified inside
    the same transaction that performs the redirect) and currently
    preferring `agent` are redirected to the other agent - a CLAUDE/CODEX
    -class task stays with its architecturally-intended agent even while
    it waits, and an IN_PROGRESS task is never touched mid-flight
    (switching executor without need is explicitly disallowed by the
    issue's own CONTEXT note). Redirection only happens when the OTHER
    agent is actually available; if both are limited, assignment is left
    alone rather than moved to an equally-unavailable destination. A
    redirected task's reviewer_preference is also fixed up when it would
    otherwise collide with the new preferred_agent (#24 requires a
    reviewer distinct from the implementer).
    """
    if agent not in (AgentName.CLAUDE, AgentName.CODEX):
        raise ValueError(f"agent must be a concrete agent, got {agent}")
    cfg = config or load_config()
    now = _now(clock)
    if reset_at is None:
        reset_at = now + timedelta(minutes=cfg.rate_limit_backoff_minutes)
    elif reset_at.utcoffset() is None:
        raise ValueError("reset_at must be a timezone-aware datetime")
    else:
        reset_at = reset_at.astimezone(timezone.utc)

    other = _opposite_agent(agent)

    def apply(connection) -> None:
        connection.execute(
            "INSERT INTO rate_limits (agent, reason, reset_at) VALUES (?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET reason=excluded.reason, reset_at=excluded.reset_at",
            (agent.value, reason, reset_at.isoformat()),
        )
        emit_in_transaction(
            connection, EventType.AGENT_RATE_LIMITED,
            {"agent": agent.value, "reason": reason, "reset_at": reset_at.isoformat()},
            created_at=now,
        )

        if not _available_in_connection(connection, other, now):
            return

        rows = connection.execute(
            "SELECT id, data FROM tasks WHERE state = ?", (TaskState.READY.value,)
        ).fetchall()
        for task_id, data in rows:
            task = Task.from_dict(json.loads(data))
            if task.agent_class != AgentClass.FLEX or task.preferred_agent != agent:
                continue
            task.preferred_agent = other
            if task.reviewer_preference == other:
                task.reviewer_preference = agent
            connection.execute(
                "UPDATE tasks SET data = ? WHERE id = ? AND state = ?",
                (json.dumps(task.to_dict()), task_id, TaskState.READY.value),
            )

    store.run_in_transaction(apply)


def is_agent_available(
    store: Store, agent: AgentName, *, clock: _DefaultClock | None = None
) -> bool:
    """True if `agent` has no active rate limit, or its cooldown already
    elapsed per `clock` - an elapsed cooldown counts as available even
    before `mark_agent_available` is called explicitly."""
    limit = store.get_rate_limit(agent.value)
    if limit is None:
        return True
    reset_at = limit.get("reset_at")
    if reset_at is None:
        # No reset time on record means the cooldown is indefinite until
        # mark_agent_available() clears it explicitly.
        return False
    return _now(clock) >= datetime.fromisoformat(reset_at)


def mark_agent_available(store: Store, agent: AgentName) -> None:
    """Clears `agent`'s rate limit and emits AGENT_AVAILABLE.

    Does not itself reassign work back to `agent`: FLEX tasks redirected
    away while it was limited are already correctly assigned to whoever
    picked them up, and any task still preferring `agent` (never touched
    because it wasn't READY, or wasn't FLEX) becomes normally available
    to it again the moment the runtime next looks for promotable work -
    no separate "resume" bookkeeping is needed here.
    """
    store.clear_rate_limit(agent.value)
    emit(store, EventType.AGENT_AVAILABLE, {"agent": agent.value})
