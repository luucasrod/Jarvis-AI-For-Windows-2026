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


# --- Regression tests from Codex's review (Review Task #56, PR #55) --------
# `since` with a non-UTC offset (e.g. Europe/Lisbon's +01:00 in summer)
# compared as a raw ISO string against UTC-stored created_at and silently
# returned the wrong result.

def test_since_with_positive_utc_offset_matches_equivalent_utc_instant(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {})

    event_time = query_events(store)[0]["created_at"]
    one_second_before_utc = event_time - timedelta(seconds=1)
    # exactly the same instant, expressed with a +01:00 offset instead of UTC
    equivalent_lisbon_summer_time = one_second_before_utc.astimezone(timezone(timedelta(hours=1)))

    assert len(query_events(store, since=one_second_before_utc)) == 1
    assert len(query_events(store, since=equivalent_lisbon_summer_time)) == 1
    store.close()


def test_since_with_negative_utc_offset_matches_equivalent_utc_instant(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {})

    event_time = query_events(store)[0]["created_at"]
    one_second_before_utc = event_time - timedelta(seconds=1)
    equivalent_negative_offset_time = one_second_before_utc.astimezone(timezone(timedelta(hours=-5)))

    assert len(query_events(store, since=equivalent_negative_offset_time)) == 1
    store.close()


def test_since_naive_datetime_is_treated_as_utc(tmp_path):
    store = Store(tmp_path / "state.db")
    emit(store, EventType.TASK_CREATED, {})

    naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    assert len(query_events(store, since=naive_past)) == 1
    store.close()


# --- created_at override (needed by #27's idle-detection tests) -----------

def test_emit_accepts_explicit_created_at(tmp_path):
    store = Store(tmp_path / "state.db")
    explicit = datetime(2026, 1, 1, tzinfo=timezone.utc)

    emit(store, EventType.TASK_CREATED, {}, created_at=explicit)

    assert query_events(store)[0]["created_at"] == explicit
    store.close()


def test_emit_normalizes_explicit_created_at_to_utc(tmp_path):
    store = Store(tmp_path / "state.db")
    lisbon_summer = datetime(2026, 6, 1, 13, 0, tzinfo=timezone(timedelta(hours=1)))

    emit(store, EventType.TASK_CREATED, {}, created_at=lisbon_summer)

    assert query_events(store)[0]["created_at"] == lisbon_summer.astimezone(timezone.utc)
    store.close()


def test_emit_rejects_naive_created_at(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        emit(store, EventType.TASK_CREATED, {}, created_at=datetime(2026, 1, 1))
        assert False, "expected ValueError"
    except ValueError as error:
        assert "timezone-aware" in str(error)
    store.close()
