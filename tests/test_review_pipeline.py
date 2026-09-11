"""Tests for orchestrator.review_pipeline (issue #29)."""
from orchestrator.events import EventType, query_events
from orchestrator.models import AgentName, Task, TaskState
from orchestrator.persistence import Store
from orchestrator.review_pipeline import ReviewOutcome, request_review


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


def test_third_failure_escalates_to_ceo_and_needs_lucas(tmp_path):
    store = Store(tmp_path / "state.db")
    task = _make_task()

    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)
    result = request_review(task, AgentName.CODEX, AgentName.CLAUDE, passed=False, store=store)

    assert result.failure_count == 3
    assert result.escalate_to_ceo is True
    assert result.needs_lucas is True
    assert result.next_implementer is None
    assert result.recommended_state == TaskState.NEEDS_LUCAS

    pending = store.get_pending_decisions()
    assert len(pending) == 1
    assert pending[0]["task_id"] == task.id

    decision_events = query_events(store, event_types=[EventType.DECISION_REQUIRED])
    assert len(decision_events) == 1
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
