"""Tests for orchestrator.history (issue #34)."""
from datetime import datetime, timedelta, timezone

from orchestrator.events import EventType, emit
from orchestrator.history import diff_since, summarize_for_voice
from orchestrator.persistence import Store


def test_diff_since_with_varied_activity(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {}, project_id="cashy")
    emit(store, EventType.TASK_CREATED, {}, project_id="argos")
    emit(store, EventType.TASK_COMPLETED, {}, project_id="cashy")
    emit(store, EventType.REVIEW_PASSED, {}, project_id="cashy")
    emit(store, EventType.REVIEW_FAILED, {}, project_id="argos")
    emit(store, EventType.BUG_FOUND, {}, project_id="argos")
    emit(store, EventType.DEPLOYMENT_FINISHED, {}, project_id="cashy")
    emit(store, EventType.MERGE_COMPLETED, {}, project_id="cashy")
    emit(store, EventType.DECISION_REQUIRED, {}, project_id="cashy", correlation_id="corr-1")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert summary.tasks_created == 2
    assert summary.tasks_completed == 1
    assert summary.reviews_passed == 1
    assert summary.reviews_failed == 1
    assert summary.bugs_found == 1
    assert summary.deployments_finished == 1
    assert summary.merges_completed == 1
    assert summary.decisions_pending == 1
    assert summary.total_events == 9
    assert summary.by_project["cashy"]["task_created"] == 1
    assert summary.by_project["argos"]["task_created"] == 1
    store.close()


# --- Regression tests from Codex's review (Review Task #68, PR #67) -------

def test_decision_requested_and_received_in_window_is_not_reported_as_pending(tmp_path):
    # A decision asked AND answered (plus the task completing) within the
    # same window must not still look like it's "esperando decisao" - the
    # old code counted raw DECISION_REQUIRED events regardless of whether
    # a DECISION_RECEIVED for the same correlation_id followed.
    store = Store(tmp_path / "state.db")
    emit(store, EventType.DECISION_REQUIRED, {}, project_id="cashy", correlation_id="corr-1")
    emit(store, EventType.DECISION_RECEIVED, {}, project_id="cashy", correlation_id="corr-1")
    emit(store, EventType.TASK_COMPLETED, {}, project_id="cashy")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert summary.decisions_pending == 0
    assert summary.tasks_completed == 1

    text = summarize_for_voice(summary)
    assert "decisao" not in text.lower() or "pendente" not in text.lower()
    store.close()


def test_decision_still_pending_when_only_requested(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.DECISION_REQUIRED, {}, project_id="cashy", correlation_id="corr-1")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert summary.decisions_pending == 1
    assert "1 decisao(oes) pendente(s)" in summarize_for_voice(summary)
    store.close()


def test_task_blocked_without_decision_is_counted_historically(tmp_path):
    # A task can be genuinely stuck (dependency/cycle) without any
    # DECISION_REQUIRED ever firing - tasks_blocked must not depend on
    # the decisions machinery at all (Review Task #68, 2nd revalidation).
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_BLOCKED, {}, project_id="cashy")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert summary.tasks_blocked == 1
    assert summary.decisions_pending == 0
    assert "1 tarefa(s) entraram em bloqueio no periodo" in summarize_for_voice(summary)
    store.close()


def test_task_blocked_then_completed_in_window_still_counts_historically(tmp_path):
    # tasks_blocked is a historical "entered blocked state" count, not a
    # claim about current state - a block followed by completion in the
    # same window still counts (Review Task #68, 2nd revalidation).
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_BLOCKED, {}, project_id="cashy")
    emit(store, EventType.TASK_COMPLETED, {}, project_id="cashy")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert summary.tasks_blocked == 1
    assert summary.tasks_completed == 1
    store.close()


def test_merge_completed_is_counted_and_reported():
    from orchestrator.history import HistorySummary

    summary = HistorySummary(since=datetime.now(timezone.utc), merges_completed=2, total_events=2)
    assert "2 merge(s) concluido(s)" in summarize_for_voice(summary)


def test_diff_since_no_activity_returns_empty_summary(tmp_path):
    store = Store(tmp_path / "state.db")
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    summary = diff_since(store, since)

    assert summary.total_events == 0
    assert summary.tasks_created == 0
    assert summary.by_project == {}
    store.close()


def test_diff_since_partial_window_excludes_older_events(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {})

    future_cutoff = datetime.now(timezone.utc) + timedelta(minutes=5)
    summary = diff_since(store, future_cutoff)

    assert summary.total_events == 0
    store.close()


def test_summarize_for_voice_no_activity():
    from orchestrator.history import HistorySummary
    summary = HistorySummary(since=datetime.now(timezone.utc))
    assert summarize_for_voice(summary) == "Nada relevante aconteceu nesse periodo."


def test_summarize_for_voice_with_activity():
    from orchestrator.history import HistorySummary
    summary = HistorySummary(
        since=datetime.now(timezone.utc),
        tasks_created=3,
        tasks_completed=2,
        total_events=5,
    )
    text = summarize_for_voice(summary)
    assert "3 tarefa(s) criada(s)" in text
    assert "2 concluida(s)" in text


def test_event_without_project_id_groups_under_sem_projeto(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {})

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert "sem_projeto" in summary.by_project
    store.close()
