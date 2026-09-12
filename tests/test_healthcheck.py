"""Tests for orchestrator.healthcheck.check_idle (issue #27)."""
from datetime import datetime, timedelta, timezone

from orchestrator.agent_availability import mark_rate_limited
from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, emit, query_events
from orchestrator.healthcheck import check_idle
from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task, TaskState
from orchestrator.persistence import Store


class Clock:
    def __init__(self, iso: str):
        self._now = datetime.fromisoformat(iso)

    def __call__(self) -> datetime:
        return self._now

    def set(self, iso: str) -> None:
        self._now = datetime.fromisoformat(iso)


CONFIG = OrchestratorConfig(idle_check_minutes=15)


def _task(**overrides) -> Task:
    defaults = dict(
        title="Some work", objective="obj", agent_class=AgentClass.FLEX,
        preferred_agent=AgentName.CLAUDE, state=TaskState.READY,
        execution_mode=ExecutionMode.PARALLEL, reviewer_preference=AgentName.CODEX,
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_no_ready_tasks_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    assert check_idle(store, config=CONFIG) is None
    store.close()


def test_task_in_progress_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    store.save_task(_task(state=TaskState.READY))
    store.save_task(_task(state=TaskState.IN_PROGRESS))

    assert check_idle(store, config=CONFIG) is None
    store.close()


def test_no_agent_available_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    store.save_task(_task())
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")
    mark_rate_limited(store, AgentName.CODEX, "usage limit")

    assert check_idle(store, config=CONFIG) is None
    store.close()


def test_within_grace_period_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    store.save_task(_task())
    emit(store, EventType.TASK_COMPLETED, {}, created_at=clock())

    clock.set("2026-09-12T10:14:59+00:00")
    assert check_idle(store, config=CONFIG, clock=clock) is None
    store.close()


def test_real_idleness_detected_and_escalated(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _task()
    store.save_task(task)
    emit(store, EventType.TASK_COMPLETED, {}, created_at=clock())

    clock.set("2026-09-12T10:15:00+00:00")
    diagnosis = check_idle(
        store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
    )

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    assert diagnosis.escalated is True
    assert diagnosis.ready_task_ids == (task.id,)

    events = query_events(store, event_types=[EventType.DECISION_REQUIRED])
    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "idle_stall"
    store.close()


def test_never_had_activity_is_immediately_eligible(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _task()
    store.save_task(task)

    diagnosis = check_idle(store, config=CONFIG, paperclip_available=lambda: True)

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    store.close()


def test_paperclip_unavailable_is_diagnosed_and_not_escalated(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _task()
    store.save_task(task)

    diagnosis = check_idle(store, config=CONFIG, paperclip_available=lambda: False)

    assert diagnosis is not None
    assert diagnosis.cause == "paperclip_unavailable"
    assert diagnosis.escalated is False
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []
    store.close()


def test_dependency_inconsistency_is_diagnosed_and_not_escalated(tmp_path):
    store = Store(tmp_path / "state.db")
    blocker = _task(state=TaskState.BLOCKED)
    store.save_task(blocker)
    stuck = _task(state=TaskState.READY, dependencies=[blocker.id])
    store.save_task(stuck)

    diagnosis = check_idle(store, config=CONFIG, paperclip_available=lambda: True)

    assert diagnosis is not None
    assert diagnosis.cause == "dependency_blocked"
    assert diagnosis.escalated is False
    assert diagnosis.ready_task_ids == (stuck.id,)
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []
    store.close()


def test_legitimately_promotable_ready_task_is_not_dependency_blocked(tmp_path):
    store = Store(tmp_path / "state.db")
    done = _task(state=TaskState.DONE)
    store.save_task(done)
    ready = _task(state=TaskState.READY, dependencies=[done.id])
    store.save_task(ready)

    diagnosis = check_idle(store, config=CONFIG, paperclip_available=lambda: True)

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    store.close()


def test_agent_available_via_expired_cooldown_still_counts(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _task(preferred_agent=AgentName.CLAUDE)
    store.save_task(task)
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit",
                       reset_at=datetime(2026, 9, 12, 10, 15, tzinfo=timezone.utc), clock=clock)
    mark_rate_limited(store, AgentName.CODEX, "usage limit",
                       reset_at=datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc), clock=clock)

    clock.set("2026-09-12T10:16:00+00:00")
    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True)

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    store.close()
