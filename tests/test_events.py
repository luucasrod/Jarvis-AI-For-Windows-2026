"""Tests for orchestrator.events (issue #14)."""
from datetime import datetime, timedelta, timezone

from orchestrator.events import EventType, emit, query_events
from orchestrator.persistence import Store


def test_emit_and_query_by_type(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {"task_id": "1"})
    emit(store, EventType.TASK_COMPLETED, {"task_id": "1"})

    created = query_events(store, event_types=[EventType.TASK_CREATED])
    assert len(created) == 1
    assert created[0]["event_type"] == EventType.TASK_CREATED
    assert created[0]["payload"] == {"task_id": "1"}
    store.close()


def test_query_all_events_chronological_order(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {"n": 1})
    emit(store, EventType.TASK_READY, {"n": 2})
    emit(store, EventType.TASK_STARTED, {"n": 3})

    events = query_events(store)
    assert [e["payload"]["n"] for e in events] == [1, 2, 3]
    store.close()


def test_query_filters_by_correlation_id(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.DECISION_REQUIRED, {}, correlation_id="corr-a")
    emit(store, EventType.DECISION_RECEIVED, {}, correlation_id="corr-b")

    matched = query_events(store, correlation_id="corr-a")
    assert len(matched) == 1
    assert matched[0]["event_type"] == EventType.DECISION_REQUIRED
    store.close()


def test_query_filters_by_project_id(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.BUG_FOUND, {}, project_id="argos")
    emit(store, EventType.BUG_FOUND, {}, project_id="cashy")

    matched = query_events(store, project_id="argos")
    assert len(matched) == 1
    assert matched[0]["project_id"] == "argos"
    store.close()


def test_query_filters_by_since(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.AGENT_RATE_LIMITED, {})

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert query_events(store, since=future) == []

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert len(query_events(store, since=past)) == 1
    store.close()


def test_emit_defaults_payload_to_empty_dict(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.AGENT_AVAILABLE)
    events = query_events(store)
    assert events[0]["payload"] == {}
    store.close()
