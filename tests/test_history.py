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
    emit(store, EventType.DECISION_REQUIRED, {}, project_id="cashy")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    summary = diff_since(store, since)

    assert summary.tasks_created == 2
    assert summary.tasks_completed == 1
    assert summary.reviews_passed == 1
    assert summary.reviews_failed == 1
    assert summary.bugs_found == 1
    assert summary.deployments_finished == 1
    assert summary.tasks_blocked == 1
    assert summary.total_events == 8
    assert summary.by_project["cashy"]["task_created"] == 1
    assert summary.by_project["argos"]["task_created"] == 1
    store.close()


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
