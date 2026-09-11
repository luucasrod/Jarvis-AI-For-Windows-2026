"""Tests for orchestrator.telegram_bot (issue #19). No real network call -
post_fn/get_fn are always injected fakes."""
from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, query_events
from orchestrator.persistence import Store
from orchestrator.telegram_bot import receive_control_updates, send_control_message, send_report_message

_CONFIGURED = OrchestratorConfig(
    telegram_bot_token="fake-token",
    telegram_control_chat_id="111",
    telegram_report_chat_id="222",
)
_UNCONFIGURED = OrchestratorConfig()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


def test_send_control_message_success():
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        return _FakeResponse(200)

    ok, error = send_control_message("oi", config=_CONFIGURED, post_fn=fake_post)

    assert ok is True
    assert error is None
    assert calls[0][1]["chat_id"] == "111"
    assert calls[0][1]["text"] == "oi"


def test_send_report_message_uses_report_chat_id():
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json)
        return _FakeResponse(200)

    send_report_message("relatorio", config=_CONFIGURED, post_fn=fake_post)
    assert calls[0]["chat_id"] == "222"


def test_send_without_credentials_is_safe_noop():
    def _should_not_be_called(*a, **kw):
        raise AssertionError("nao deveria chamar a rede sem credenciais")

    ok, error = send_control_message("oi", config=_UNCONFIGURED, post_fn=_should_not_be_called)

    assert ok is False
    assert "nao configurado" in error


def test_send_invalid_token_returns_error_not_exception():
    def fake_post(url, json, timeout):
        return _FakeResponse(401)

    ok, error = send_control_message("oi", config=_CONFIGURED, post_fn=fake_post)
    assert ok is False
    assert "invalido" in error


def test_send_network_timeout_handled_gracefully():
    import requests

    def fake_post(url, json, timeout):
        raise requests.exceptions.Timeout()

    ok, error = send_control_message("oi", config=_CONFIGURED, post_fn=fake_post)
    assert ok is False
    assert error == "timeout"


def test_send_connection_error_handled_gracefully():
    import requests

    def fake_post(url, json, timeout):
        raise requests.exceptions.ConnectionError()

    ok, error = send_control_message("oi", config=_CONFIGURED, post_fn=fake_post)
    assert ok is False
    assert error == "offline"


def test_receive_control_updates_emits_event_for_new_message(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [
                {"update_id": 5001, "message": {"chat": {"id": 111}, "text": "Quero que o Argos faca X"}}
            ]
        })

    new_offset = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get)

    assert new_offset == 5001
    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "Quero que o Argos faca X"
    store.close()


def test_receive_control_updates_ignores_other_chats(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [
                {"update_id": 1, "message": {"chat": {"id": 999}, "text": "mensagem de outro chat"}}
            ]
        })

    receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get)

    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert events == []
    store.close()


def test_receive_control_updates_without_credentials_is_noop(tmp_path):
    store = Store(tmp_path / "state.db")

    def _should_not_be_called(*a, **kw):
        raise AssertionError("nao deveria chamar a rede sem credenciais")

    result = receive_control_updates(store, config=_UNCONFIGURED, get_fn=_should_not_be_called, last_update_id=42)
    assert result == 42
    store.close()


def test_receive_control_updates_network_failure_returns_unchanged_offset(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        raise Exception("boom")

    result = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    assert result == 10
    store.close()


def test_receive_control_updates_offset_advances_for_next_call(tmp_path):
    store = Store(tmp_path / "state.db")
    seen_params = []

    def fake_get(url, params, timeout):
        seen_params.append(params)
        return _FakeResponse(200, {"result": []})

    receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=99)
    assert seen_params[0]["offset"] == 100
    store.close()
