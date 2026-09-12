"""Rate-limit detection, cooldown and FLEX task redirection (issue #26).

Separate from #24's agent_policy: that module produces the INITIAL
classification of a task; this module reacts when an agent already
assigned to work becomes temporarily unavailable. Detection of the real
rate-limit signal from each agent's own API/CLI is explicitly out of
scope (it depends on how Paperclip/the agent CLI eventually exposes that
error) - this module is the interface/reaction layer: record the
cooldown, answer availability queries, and redirect only what's safe to
redirect (READY FLEX tasks, never IN_PROGRESS work).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit
from orchestrator.models import AgentClass, AgentName, TaskState
from orchestrator.persistence import Store

_DefaultClock = Callable[[], datetime]


def _opposite_agent(agent: AgentName) -> AgentName:
    return AgentName.CODEX if agent == AgentName.CLAUDE else AgentName.CLAUDE


def _now(clock: _DefaultClock | None) -> datetime:
    instant = (clock or (lambda: datetime.now(timezone.utc)))()
    if instant.utcoffset() is None:
        raise ValueError("agent_availability clock must return a timezone-aware datetime")
    return instant.astimezone(timezone.utc)


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
    FLEX-class tasks still in READY (never started) and currently
    preferring `agent` are redirected to the other agent - a CLAUDE/CODEX
    -class task stays with its architecturally-intended agent even while
    it waits, and an IN_PROGRESS task is never touched mid-flight
    (switching executor without need is explicitly disallowed by the
    issue's own CONTEXT note).
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

    store.set_rate_limit(agent.value, reason, reset_at.isoformat())
    emit(
        store,
        EventType.AGENT_RATE_LIMITED,
        {"agent": agent.value, "reason": reason, "reset_at": reset_at.isoformat()},
    )

    other = _opposite_agent(agent)
    for task in store.list_tasks(TaskState.READY):
        if task.agent_class == AgentClass.FLEX and task.preferred_agent == agent:
            task.preferred_agent = other
            store.save_task(task)


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
