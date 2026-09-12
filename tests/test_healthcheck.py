"""Tests for orchestrator.healthcheck.check_idle (issue #27).

Several scenarios here are regressions from Codex's review (Review Task
#113): a single blocked READY task hiding otherwise-idle free work, a
brand-new database with no activity events being read as "idle forever"
instead of getting a grace period, duplicate DECISION_REQUIRED escalations
on every poll of the same stall, and a paused Paperclip agent never being
considered as a probable cause.
"""
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
NO_PAUSED_AGENTS = {"available": True, "companies": []}


def _task(*, updated_at: datetime, **overrides) -> Task:
    defaults = dict(
        title="Some work", objective="obj", agent_class=AgentClass.FLEX,
        preferred_agent=AgentName.CLAUDE, state=TaskState.READY,
        execution_mode=ExecutionMode.PARALLEL, reviewer_preference=AgentName.CODEX,
        updated_at=updated_at,
    )
    defaults.update(overrides)
    return Task(**defaults)


def _stale(clock: Clock, **overrides) -> Task:
    """A task whose own updated_at is already well past the grace period
    relative to `clock`'s current instant - the common case in these
    tests, where the scenario under test is the STALL itself, not the
    grace period's own boundary."""
    return _task(updated_at=clock() - timedelta(minutes=CONFIG.idle_check_minutes + 1), **overrides)


def test_no_ready_tasks_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    assert check_idle(store, config=CONFIG) is None
    store.close()


def test_task_in_progress_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    store.save_task(_stale(clock, state=TaskState.READY))
    store.save_task(_stale(clock, state=TaskState.IN_PROGRESS))

    assert check_idle(store, config=CONFIG, clock=clock) is None
    store.close()


def test_no_agent_available_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    store.save_task(_stale(clock))
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit", clock=clock)
    mark_rate_limited(store, AgentName.CODEX, "usage limit", clock=clock)

    assert check_idle(store, config=CONFIG, clock=clock) is None
    store.close()


def test_within_grace_period_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    store.save_task(_task(updated_at=clock()))
    emit(store, EventType.TASK_COMPLETED, {}, created_at=clock())

    clock.set("2026-09-12T10:14:59+00:00")
    assert check_idle(store, config=CONFIG, clock=clock) is None
    store.close()


def test_real_idleness_detected_and_escalated(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _task(updated_at=clock())
    store.save_task(task)
    emit(store, EventType.TASK_COMPLETED, {}, created_at=clock())

    clock.set("2026-09-12T10:15:00+00:00")
    diagnosis = check_idle(
        store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
        paperclip_snapshot=lambda: NO_PAUSED_AGENTS,
    )

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    assert diagnosis.escalated is True
    assert diagnosis.ready_task_ids == (task.id,)

    events = query_events(store, event_types=[EventType.DECISION_REQUIRED])
    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "idle_stall"
    store.close()


def test_repeated_poll_of_same_stall_escalates_only_once(tmp_path):
    # Regression (finding #3): a periodic poller must not re-ask the same
    # question on every tick while nothing about the stall changed.
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _task(updated_at=clock())
    store.save_task(task)

    clock.set("2026-09-12T10:16:00+00:00")
    first = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                       paperclip_snapshot=lambda: NO_PAUSED_AGENTS)
    clock.set("2026-09-12T10:20:00+00:00")
    second = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                        paperclip_snapshot=lambda: NO_PAUSED_AGENTS)

    assert first is not None and second is not None
    assert first.cause == second.cause == "unexplained"
    events = query_events(store, event_types=[EventType.DECISION_REQUIRED])
    assert len(events) == 1
    store.close()


def test_new_stalled_task_set_is_a_new_episode_and_escalates_again(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    first_task = _task(updated_at=clock())
    store.save_task(first_task)

    clock.set("2026-09-12T10:16:00+00:00")
    check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
              paperclip_snapshot=lambda: NO_PAUSED_AGENTS)

    second_task = _task(updated_at=clock())
    store.save_task(second_task)
    clock.set("2026-09-12T10:32:00+00:00")
    check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
              paperclip_snapshot=lambda: NO_PAUSED_AGENTS)

    events = query_events(store, event_types=[EventType.DECISION_REQUIRED])
    assert len(events) == 2
    store.close()


def test_no_prior_activity_still_gets_a_grace_period(tmp_path):
    # Regression (finding #2): a freshly-created READY task with zero
    # event history must not be read as "idle forever" - its own
    # updated_at is the grace period's anchor.
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _task(updated_at=clock())
    store.save_task(task)

    clock.set("2026-09-12T10:14:59+00:00")
    assert check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True) is None

    clock.set("2026-09-12T10:15:00+00:00")
    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                           paperclip_snapshot=lambda: NO_PAUSED_AGENTS)
    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    store.close()


def test_paperclip_unavailable_is_diagnosed_and_not_escalated(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _stale(clock)
    store.save_task(task)

    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: False)

    assert diagnosis is not None
    assert diagnosis.cause == "paperclip_unavailable"
    assert diagnosis.escalated is False
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []
    store.close()


def test_paused_agent_is_diagnosed_as_probable_cause_and_not_escalated(tmp_path):
    # Regression (finding #4): a paused CEO/agent in Paperclip's own
    # snapshot is a probable, explained cause - never an "unexplained"
    # escalation, and never an attempt to auto-resume it (see docstring).
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _stale(clock)
    store.save_task(task)
    snapshot = {
        "available": True,
        "companies": [{"agents": [{"name": "CEO", "status": "paused", "pause_reason": "budget"}]}],
    }

    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                           paperclip_snapshot=lambda: snapshot)

    assert diagnosis is not None
    assert diagnosis.cause == "agent_paused"
    assert diagnosis.escalated is False
    assert "CEO" in diagnosis.detail
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []
    store.close()


def test_dependency_inconsistency_is_diagnosed_and_not_escalated(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    blocker = _task(updated_at=clock(), state=TaskState.BLOCKED)
    store.save_task(blocker)
    stuck = _stale(clock, state=TaskState.READY, dependencies=[blocker.id])
    store.save_task(stuck)

    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True)

    assert diagnosis is not None
    assert diagnosis.cause == "dependency_blocked"
    assert diagnosis.escalated is False
    assert diagnosis.ready_task_ids == (stuck.id,)
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []
    store.close()


def test_one_blocked_ready_task_never_hides_other_free_ready_work(tmp_path):
    # Regression (finding #1): the ISSUE says "dependencia bloqueando
    # TUDO" - one blocked task must not swallow the diagnosis for
    # another READY task that has nothing stopping it.
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    blocker = _stale(clock, state=TaskState.BLOCKED)
    store.save_task(blocker)
    stuck = _stale(clock, state=TaskState.READY, dependencies=[blocker.id])
    store.save_task(stuck)
    free = _stale(clock, state=TaskState.READY)
    store.save_task(free)

    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                           paperclip_snapshot=lambda: NO_PAUSED_AGENTS)

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    assert diagnosis.escalated is True
    assert diagnosis.ready_task_ids == (free.id,)
    store.close()


def test_legitimately_promotable_ready_task_is_not_dependency_blocked(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    done = _stale(clock, state=TaskState.DONE)
    store.save_task(done)
    ready = _stale(clock, state=TaskState.READY, dependencies=[done.id])
    store.save_task(ready)

    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                           paperclip_snapshot=lambda: NO_PAUSED_AGENTS)

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    store.close()


def test_agent_available_via_expired_cooldown_still_counts(tmp_path):
    store = Store(tmp_path / "state.db")
    clock = Clock("2026-09-12T10:00:00+00:00")
    task = _task(updated_at=clock(), preferred_agent=AgentName.CLAUDE)
    store.save_task(task)
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit",
                       reset_at=datetime(2026, 9, 12, 10, 15, tzinfo=timezone.utc), clock=clock)
    mark_rate_limited(store, AgentName.CODEX, "usage limit",
                       reset_at=datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc), clock=clock)

    clock.set("2026-09-12T10:16:00+00:00")
    diagnosis = check_idle(store, config=CONFIG, clock=clock, paperclip_available=lambda: True,
                           paperclip_snapshot=lambda: NO_PAUSED_AGENTS)

    assert diagnosis is not None
    assert diagnosis.cause == "unexplained"
    store.close()
