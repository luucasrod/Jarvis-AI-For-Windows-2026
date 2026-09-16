"""Tests for orchestrator.runtime (issue #149 follow-up: the actual
polling loop that makes #31/#148/#149 reachable outside a one-off script).
"""
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.persistence import Store
from orchestrator.runtime import run_forever

_CONFIGURED = OrchestratorConfig(
    telegram_bot_token="fake-token",
    telegram_control_chat_id="111",
    telegram_report_chat_id="222",
)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {"result": []}

    def json(self):
        return self._json_data


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


def test_incoming_objective_is_processed_and_answered(store):
    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            return _FakeResponse(200, {
                "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "Objetivo: testar"}}]
            })
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    sent = []
    run_forever(
        store=store, config=_CONFIGURED, iterations=1,
        get_fn=fake_get,
        send_fn=lambda text, **k: (sent.append(text), (True, None))[1],
        fallback_fn=lambda text: pytest.fail("must not reach free conversation for a structured command"),
    )

    assert len(sent) == 1


def test_unrecognized_text_reaches_the_free_conversation_fallback(store):
    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            return _FakeResponse(200, {
                "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "oi tudo bem?"}}]
            })
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    seen_fallback = []
    sent = []
    run_forever(
        store=store, config=_CONFIGURED, iterations=1,
        get_fn=fake_get,
        send_fn=lambda text, **k: (sent.append(text), (True, None))[1],
        fallback_fn=lambda text: (seen_fallback.append(text), "resposta livre")[1],
    )

    assert seen_fallback == ["oi tudo bem?"]
    assert sent == ["resposta livre"]


def test_confirm_fn_is_threaded_through_to_handle_control_message(store):
    # Issue #152: a "sim" that resolves a pending plan must reach the
    # SAME confirm_fn the caller wired into run_forever, not a default.
    # Seeds a pending plan directly (bypassing the real planner, which
    # needs a real LLM/project index - out of scope for this unit test).
    from orchestrator.decisions import save_pending_plan
    save_pending_plan(store, objective="testar", project_id="hub", task_ids=["t1"])

    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            return _FakeResponse(200, {
                "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "sim"}}]
            })
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    seen = []
    run_forever(
        store=store, config=_CONFIGURED, iterations=1, get_fn=fake_get,
        send_fn=lambda text, **k: (True, None),
        confirm_fn=lambda pending_plan: (seen.append(pending_plan), "ok")[1],
    )

    assert seen == [{"objective": "testar", "project_id": "hub", "task_ids": ["t1"]}]


def test_no_new_messages_never_calls_fallback_or_send(store):
    def fake_get(url, params, timeout):
        return _FakeResponse(200, {"result": []})

    run_forever(
        store=store, config=_CONFIGURED, iterations=1, get_fn=fake_get,
        send_fn=lambda text, **k: pytest.fail("must not send anything"),
        fallback_fn=lambda text: pytest.fail("must not be called"),
    )


def test_two_messages_in_one_poll_are_both_processed_exactly_once(store):
    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            return _FakeResponse(200, {
                "result": [
                    {"update_id": 1, "message": {"chat": {"id": 111}, "text": "primeira pergunta"}},
                    {"update_id": 2, "message": {"chat": {"id": 111}, "text": "segunda pergunta"}},
                ]
            })
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    seen = []
    run_forever(
        store=store, config=_CONFIGURED, iterations=1, get_fn=fake_get,
        send_fn=lambda text, **k: (True, None),
        fallback_fn=lambda text: (seen.append(text), "ok")[1],
    )

    assert seen == ["primeira pergunta", "segunda pergunta"]


def test_second_poll_never_reprocesses_messages_from_the_first(store):
    calls = {"n": 0}

    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(200, {
                    "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "pergunta unica"}}]
                })
            return _FakeResponse(200, {"result": []})
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    seen = []
    run_forever(
        store=store, config=_CONFIGURED, iterations=2, poll_interval_seconds=0, get_fn=fake_get,
        send_fn=lambda text, **k: (True, None),
        fallback_fn=lambda text: (seen.append(text), "ok")[1],
    )

    assert seen == ["pergunta unica"]


def test_fallback_exception_does_not_stop_the_loop(store):
    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            return _FakeResponse(200, {
                "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "vai explodir"}}]
            })
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    def boom(text):
        raise RuntimeError("simulated failure")

    # Must not raise out of run_forever.
    run_forever(
        store=store, config=_CONFIGURED, iterations=1, get_fn=fake_get,
        send_fn=lambda text, **k: (True, None), fallback_fn=boom,
    )


def test_getupdates_network_failure_does_not_stop_the_loop(store):
    def fake_get(url, params, timeout):
        raise ConnectionError("offline")

    run_forever(store=store, config=_CONFIGURED, iterations=1, get_fn=fake_get)


def test_stale_event_returned_alongside_a_new_one_is_not_reprocessed(store, monkeypatch):
    # Independent-review finding: the two prior "no reprocessing" tests
    # were vacuous - neither actually exercised the update_id <=
    # previous_update_id filter inside _process_new_control_messages.
    # This calls it directly with query_events returning BOTH a stale
    # event (update_id already handled in a prior iteration) and a
    # genuinely new one in the SAME call - the real scenario the filter
    # exists for (a `since` timestamp window that overlaps a
    # previously-processed event, e.g. clock resolution/backwards jump).
    from datetime import datetime, timezone
    from orchestrator.runtime import _process_new_control_messages

    def fake_query_events(store, since, event_types):
        return [
            {"payload": {"update_id": 3, "text": "mensagem antiga ja processada"}},
            {"payload": {"update_id": 5, "text": "mensagem nova"}},
        ]

    monkeypatch.setattr("orchestrator.runtime.query_events", fake_query_events)

    seen = []
    _process_new_control_messages(
        store, _CONFIGURED, poll_started_at=datetime.now(timezone.utc), previous_update_id=3,
        send_fn=lambda text, **k: (True, None),
        fallback_fn=lambda text: (seen.append(text), "ok")[1],
    )

    assert seen == ["mensagem nova"]


def test_query_events_failure_does_not_stop_the_loop(store, monkeypatch):
    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "oi"}}]
        })

    def fake_query_events(store, since, event_types):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("orchestrator.runtime.query_events", fake_query_events)

    # Must not raise out of run_forever, and must still reach the report tick.
    calls = []
    monkeypatch.setattr(
        "orchestrator.runtime.process_report_tick",
        lambda store, **kwargs: calls.append(True),
    )
    run_forever(store=store, config=_CONFIGURED, iterations=1, get_fn=fake_get)
    assert calls == [True]


def test_a_batch_that_failed_to_process_is_retried_on_the_next_poll(store, monkeypatch):
    # Independent-review finding: query_events failing must not
    # permanently strand that batch - last_update_id rolls back so the
    # SAME Telegram batch is re-fetched and retried next iteration,
    # instead of update_id <= previous_update_id silently excluding it
    # forever once last_update_id had already moved past it.
    def fake_get(url, params, timeout):
        if "getUpdates" in url:
            return _FakeResponse(200, {
                "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "mensagem importante"}}]
            })
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    attempts = {"n": 0}
    def flaky_query_events(store, since, event_types):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("database is locked")
        return [{"payload": {"update_id": 1, "text": "mensagem importante"}}]

    monkeypatch.setattr("orchestrator.runtime.query_events", flaky_query_events)

    seen = []
    run_forever(
        store=store, config=_CONFIGURED, iterations=2, poll_interval_seconds=0, get_fn=fake_get,
        send_fn=lambda text, **k: (True, None),
        fallback_fn=lambda text: (seen.append(text), "ok")[1],
    )

    assert seen == ["mensagem importante"]


def test_report_tick_is_ticked_every_iteration(store, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "orchestrator.runtime.process_report_tick",
        lambda store, **kwargs: calls.append(kwargs.get("config")),
    )

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {"result": []})

    run_forever(store=store, config=_CONFIGURED, iterations=2, poll_interval_seconds=0, get_fn=fake_get)

    assert len(calls) == 2
