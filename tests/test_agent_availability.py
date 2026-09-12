"""Tests for orchestrator.agent_availability (issue #26)."""
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.agent_availability import (
    is_agent_available,
    mark_agent_available,
    mark_rate_limited,
)
from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, query_events
from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task, TaskState
from orchestrator.persistence import Store


class Clock:
    def __init__(self, iso: str):
        self._now = datetime.fromisoformat(iso)

    def __call__(self) -> datetime:
        return self._now

    def set(self, iso: str) -> None:
        self._now = datetime.fromisoformat(iso)


def _flex_task(**overrides):
    defaults = dict(
        title="Flex work", objective="obj", agent_class=AgentClass.FLEX,
        preferred_agent=AgentName.CLAUDE, state=TaskState.READY,
        execution_mode=ExecutionMode.PARALLEL, reviewer_preference=AgentName.CODEX,
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_mark_rate_limited_persists_and_emits_event(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit", clock=clock)

    assert is_agent_available(store, AgentName.CLAUDE, clock=clock) is False
    events = query_events(store, event_types=[EventType.AGENT_RATE_LIMITED])
    assert len(events) == 1
    assert events[0]["payload"]["agent"] == "Claude"
    store.close()


def test_default_cooldown_is_config_backoff_minutes(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    config = OrchestratorConfig(rate_limit_backoff_minutes=45)

    mark_rate_limited(store, AgentName.CODEX, "usage limit", config=config, clock=clock)

    clock.set("2026-09-12T10:44:59+00:00")
    assert is_agent_available(store, AgentName.CODEX, clock=clock) is False
    clock.set("2026-09-12T10:45:00+00:00")
    assert is_agent_available(store, AgentName.CODEX, clock=clock) is True
    store.close()


def test_explicit_reset_at_resumes_exactly_then(tmp_path):
    store = Store(tmp_path / "state.db")
    reset_at = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    clock = Clock("2026-09-12T10:00:00+00:00")

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit", reset_at=reset_at, clock=clock)

    clock.set("2026-09-12T11:59:59+00:00")
    assert is_agent_available(store, AgentName.CLAUDE, clock=clock) is False
    clock.set("2026-09-12T12:00:00+00:00")
    assert is_agent_available(store, AgentName.CLAUDE, clock=clock) is True
    store.close()


def test_naive_reset_at_rejected(tmp_path):
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError, match="timezone-aware"):
        mark_rate_limited(store, AgentName.CLAUDE, "x", reset_at=datetime(2026, 9, 12, 12, 0))
    store.close()


def test_flex_ready_task_is_redirected_to_other_agent(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CODEX
    store.close()


def test_in_progress_flex_task_is_never_touched(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE, state=TaskState.IN_PROGRESS)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CLAUDE
    store.close()


def test_non_flex_ready_task_is_never_redirected(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(agent_class=AgentClass.CLAUDE, preferred_agent=AgentName.CLAUDE)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CLAUDE
    store.close()


def test_ready_flex_task_preferring_other_agent_is_untouched(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CODEX)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CODEX
    store.close()


def test_mark_agent_available_clears_limit_and_emits_event(tmp_path):
    store = Store(tmp_path / "state.db")
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")
    assert is_agent_available(store, AgentName.CLAUDE) is False

    mark_agent_available(store, AgentName.CLAUDE)

    assert is_agent_available(store, AgentName.CLAUDE) is True
    events = query_events(store, event_types=[EventType.AGENT_AVAILABLE])
    assert len(events) == 1
    assert events[0]["payload"]["agent"] == "Claude"
    store.close()


def test_agent_with_no_recorded_limit_is_available(tmp_path):
    store = Store(tmp_path / "state.db")
    assert is_agent_available(store, AgentName.CLAUDE) is True
    assert is_agent_available(store, AgentName.CODEX) is True
    store.close()


def test_two_agents_rate_limits_are_independent(tmp_path):
    store = Store(tmp_path / "state.db")
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")
    assert is_agent_available(store, AgentName.CLAUDE) is False
    assert is_agent_available(store, AgentName.CODEX) is True
    store.close()


def test_mark_rate_limited_rejects_non_concrete_agent(tmp_path):
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError):
        mark_rate_limited(store, AgentName.EITHER, "x")
    store.close()
