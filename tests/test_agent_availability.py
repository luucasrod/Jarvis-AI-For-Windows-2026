"""Tests for orchestrator.agent_availability (issue #26).

The concurrency and reviewer-collision tests below are regressions from
Codex's review (Review Task #109, 3 P1 findings): a read-then-write race
between redirecting a task and another connection starting it, redirecting
to a destination agent that's ALSO rate-limited, and a redirect that could
leave preferred_agent == reviewer_preference (self-review).
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

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


# --- Regressions from Codex's review (Review Task #109) -------------------

def test_task_started_by_another_connection_before_redirect_is_never_reverted(tmp_path):
    # Finding #1: the old code read READY tasks and redirected them as two
    # separate Store calls (store.list_tasks(READY) then store.save_task).
    # If another connection had ALREADY started one of those tasks
    # (READY -> IN_PROGRESS) by the time the redirect ran, the redirect's
    # write - built from its now-stale in-memory snapshot, whose OWN
    # `state` field still read READY - would overwrite the row's `state`
    # column back to READY via save_task's unconditional
    # `state=excluded.state`, erasing the real IN_PROGRESS transition.
    # This reproduces that ordering directly and deterministically (no
    # thread-timing dependency): the fix's SELECT runs fresh INSIDE
    # mark_rate_limited's own transaction, so it must see IN_PROGRESS and
    # never touch this task's row at all.
    path = tmp_path / "state.db"
    task = _flex_task(preferred_agent=AgentName.CLAUDE)
    other_connection = Store(path)
    other_connection.save_task(task)

    started = other_connection.get_task(task.id)
    started.state = TaskState.IN_PROGRESS
    other_connection.save_task(started)
    other_connection.close()

    store = Store(path)
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.state == TaskState.IN_PROGRESS
    assert reloaded.preferred_agent == AgentName.CLAUDE
    store.close()


def test_concurrent_redirect_and_task_start_never_deadlocks_or_corrupts(tmp_path):
    # A genuine cross-connection concurrency run (BEGIN IMMEDIATE
    # serializes the two writers - same pattern as #17's
    # test_concurrent_connections_create_one_issue): whichever operation's
    # transaction commits first is a legitimate outcome either way, so
    # this only asserts the invariants that must ALWAYS hold regardless
    # of ordering - no exception/deadlock, the task always ends up
    # IN_PROGRESS (start_task always runs), and preferred_agent is
    # whichever concrete agent is consistent with that ordering.
    path = tmp_path / "state.db"
    task = _flex_task(preferred_agent=AgentName.CLAUDE)
    setup_store = Store(path)
    setup_store.save_task(task)
    setup_store.close()

    store_a, store_b = Store(path), Store(path)
    barrier = Barrier(2)

    def redirect():
        barrier.wait(timeout=5)
        mark_rate_limited(store_a, AgentName.CLAUDE, "usage limit")

    def start_task():
        barrier.wait(timeout=5)
        def apply(connection):
            row = connection.execute("SELECT data FROM tasks WHERE id = ?", (task.id,)).fetchone()
            current = Task.from_dict(json.loads(row[0]))
            current.state = TaskState.IN_PROGRESS
            connection.execute(
                "UPDATE tasks SET data = ?, state = ? WHERE id = ?",
                (json.dumps(current.to_dict()), TaskState.IN_PROGRESS.value, task.id),
            )
        store_b.run_in_transaction(apply)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda fn: fn(), [redirect, start_task]))

        final = store_a.get_task(task.id)
        assert final.state == TaskState.IN_PROGRESS
        assert final.preferred_agent in (AgentName.CLAUDE, AgentName.CODEX)
    finally:
        store_a.close()
        store_b.close()


def test_redirect_does_not_move_to_an_equally_limited_destination(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE)
    store.save_task(task)
    clock = Clock("2026-09-12T10:00:00+00:00")
    mark_rate_limited(store, AgentName.CODEX, "usage limit", clock=clock)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit", clock=clock)

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CLAUDE
    store.close()


def test_redirect_happens_once_destination_cooldown_expires(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE)
    store.save_task(task)
    clock = Clock("2026-09-12T10:00:00+00:00")
    mark_rate_limited(store, AgentName.CODEX, "usage limit",
                      reset_at=datetime(2026, 9, 12, 10, 5, tzinfo=timezone.utc), clock=clock)

    clock.set("2026-09-12T10:05:00+00:00")
    mark_rate_limited(store, AgentName.CLAUDE, "usage limit", clock=clock)

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CODEX
    store.close()


def test_redirect_fixes_reviewer_collision_claude_to_codex(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE, reviewer_preference=AgentName.CODEX)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CODEX
    assert reloaded.reviewer_preference == AgentName.CLAUDE
    store.close()


def test_redirect_fixes_reviewer_collision_codex_to_claude(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CODEX, reviewer_preference=AgentName.CLAUDE)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CODEX, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CLAUDE
    assert reloaded.reviewer_preference == AgentName.CODEX
    store.close()


def test_redirect_leaves_non_colliding_reviewer_untouched(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE, reviewer_preference=AgentName.EITHER)
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.preferred_agent == AgentName.CODEX
    assert reloaded.reviewer_preference == AgentName.EITHER
    store.close()


def test_redirect_preserves_class_solo_and_dependencies(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _flex_task(preferred_agent=AgentName.CLAUDE, execution_mode=ExecutionMode.SOLO,
                      reviewer_preference=AgentName.CODEX, dependencies=["dep-1"])
    store.save_task(task)

    mark_rate_limited(store, AgentName.CLAUDE, "usage limit")

    reloaded = store.get_task(task.id)
    assert reloaded.agent_class == AgentClass.FLEX
    assert reloaded.execution_mode == ExecutionMode.SOLO
    assert reloaded.dependencies == ["dep-1"]
    store.close()
