"""Tests for orchestrator.review_pipeline (issue #29)."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest

from orchestrator.events import EventType, query_events
from orchestrator.models import AgentName, Task, TaskState
from orchestrator.persistence import Store
from orchestrator.review_pipeline import ReviewOutcome, escalate_to_human, request_review


def _make_task(**overrides):
    defaults = dict(title="Implementar X", objective="obj", fallback_agent=AgentName.CODEX)
    defaults.update(overrides)
    return Task(**defaults)


def test_pass_clears_failures_and_recommends_done(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=True, store=store)

    assert result.outcome == ReviewOutcome.PASS
    assert result.failure_count == 0
    assert result.escalate_to_ceo is False
    assert result.needs_lucas is False
    assert result.recommended_state == TaskState.DONE

    events = query_events(store, event_types=[EventType.REVIEW_PASSED])
    assert len(events) == 1
    store.close()


def test_first_failure_returns_to_same_implementer(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store, feedback="faltou teste X")

    assert result.outcome == ReviewOutcome.FAIL
    assert result.failure_count == 1
    assert result.next_implementer == AgentName.CODEX
    assert result.escalate_to_ceo is False
    assert result.recommended_state == TaskState.IN_PROGRESS
    store.close()


def test_second_failure_switches_to_fallback_agent(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task(fallback_agent=AgentName.CLAUDE)

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)

    assert result.failure_count == 2
    assert result.next_implementer == AgentName.CLAUDE
    assert result.escalate_to_ceo is False
    store.close()


def test_second_failure_with_no_fallback_uses_opposite_agent(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task(fallback_agent=AgentName.NONE)

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)

    assert result.next_implementer == AgentName.CLAUDE
    store.close()


def test_third_failure_waits_for_ceo_before_creating_human_decision(tmp_path):
    # Review Task #66 (3rd pass): the 3rd failure used to page a human
    # immediately. It must instead signal the CEO first and leave the
    # human decision queue untouched until escalate_to_human() is called
    # explicitly, once diagnosis concludes a human is actually needed.
    store = Store(tmp_path / "state.db")
    task = _make_task()

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)

    assert result.failure_count == 3
    assert result.escalate_to_ceo is True
    assert result.needs_lucas is False
    assert result.next_implementer is None
    assert result.recommended_state != TaskState.NEEDS_LUCAS
    assert result.recommended_state == TaskState.BLOCKED

    assert store.get_pending_decisions() == []
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []

    ceo_events = query_events(store, event_types=[EventType.CEO_ESCALATION_REQUIRED])
    assert len(ceo_events) == 1
    store.close()


def test_escalate_to_human_creates_decision_after_ceo_diagnosis(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    for _ in range(3):
        request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)

    result = escalate_to_human(task, store, reason="CEO nao conseguiu decompor o problema")

    assert result.needs_lucas is True
    assert result.escalate_to_ceo is False
    assert result.recommended_state == TaskState.NEEDS_LUCAS

    pending = store.get_pending_decisions()
    assert len(pending) == 1
    assert pending[0]["task_id"] == task.id

    decision_events = query_events(store, event_types=[EventType.DECISION_REQUIRED])
    assert len(decision_events) == 1
    store.close()


def test_self_review_is_rejected(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    with pytest.raises(ValueError):
        request_review(task, AgentName.CODEX, AgentName.CODEX, passed=True, store=store)
    store.close()


@pytest.mark.parametrize("bad_agent", [AgentName.EITHER, AgentName.NONE])
def test_review_rejects_either_or_none_as_implementer_or_reviewer(tmp_path, bad_agent):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    with pytest.raises(ValueError):
        request_review(task, bad_agent, AgentName.CLAUDE, passed=True, store=store)
    with pytest.raises(ValueError):
        request_review(task, AgentName.CLAUDE, bad_agent, passed=True, store=store)
    store.close()


def test_second_failure_falls_through_to_opposite_when_fallback_equals_actual_implementer(tmp_path):
    # preferred=Claude, fallback=Codex, but Codex already implemented as
    # the fallback for failure #1 - a 2nd failure must switch AWAY from
    # Codex (the agent actually failing again), not blindly re-select
    # task.fallback_agent and return the same agent (Review Task #66).
    store = Store(tmp_path / "state.db")
    task = _make_task(preferred_agent=AgentName.CLAUDE, fallback_agent=AgentName.CODEX)

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)

    assert result.next_implementer == AgentName.CLAUDE
    store.close()


def test_concurrent_failures_do_not_lose_an_increment(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    import orchestrator.review_pipeline as pipeline

    original = pipeline._get_failure_count
    barrier = Barrier(2)

    def read_then_sync(*args):
        count = original(*args)
        barrier.wait(timeout=5)
        return count

    # _get_failure_count isn't on the hot path anymore (increment is
    # atomic), but patching it lets the test force both threads to
    # interleave around the same starting point, exercising the race
    # that a naive get-then-set would lose.
    with patch.object(pipeline, "_get_failure_count", read_then_sync):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(request_review, task, AgentName.CODEX, AgentName.CLAUDE, False, store)
                for _ in range(2)
            ]
            results = [f.result() for f in futures]

    assert sorted(r.failure_count for r in results) == [1, 2]
    assert original(store, task.id) == 2
    store.close()


def test_pass_after_prior_failures_resets_counter(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=True, store=store)

    assert result.outcome == ReviewOutcome.PASS
    assert result.failure_count == 0

    # A failure recorded AFTER a pass starts counting from 1 again, not 2.
    result2 = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    assert result2.failure_count == 1
    store.close()


def test_review_requested_event_always_emitted(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=True, store=store)

    events = query_events(store, event_types=[EventType.REVIEW_REQUESTED])
    assert len(events) == 1
    store.close()
