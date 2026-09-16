"""Tests for orchestrator.telegram_bot (issue #19). No real network call -
post_fn/get_fn are always injected fakes."""
import pytest

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
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {'ok': True, 'result': {'message_id': 1}}
        self.content = content

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


def test_send_and_poll_timeouts_come_from_config():
    # Issue #44: these used to be hardcoded 10/15 constants regardless of
    # config - now sourced from telegram_send_timeout_seconds /
    # telegram_poll_timeout_seconds.
    cfg = OrchestratorConfig(
        telegram_bot_token="fake-token", telegram_control_chat_id="111", telegram_report_chat_id="222",
        telegram_send_timeout_seconds=3, telegram_poll_timeout_seconds=44,
    )
    seen = {}

    def fake_post(url, json, timeout):
        seen["send"] = timeout
        return _FakeResponse()

    def fake_get(url, params, timeout):
        seen["poll"] = timeout
        return _FakeResponse(json_data={"ok": True, "result": []})

    send_control_message("oi", config=cfg, post_fn=fake_post)
    receive_control_updates(Store(":memory:"), config=cfg, get_fn=fake_get)

    assert seen["send"] == 3
    assert seen["poll"] == 44


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


# --- Regression tests from Codex's review (Review Task #70, PR #69) -------
# receive_control_updates promised never to propagate a Telegram failure,
# but only validated JSON decoding - a malformed-but-valid-JSON payload
# used to raise AttributeError deep in the loop and lose the polling
# cursor for every update after the bad one.

class _RawJsonResponse:
    """Like _FakeResponse, but returns exactly what's given - including
    None - instead of _FakeResponse's `json_data or {}` normalization."""

    def __init__(self, status_code, json_value):
        self.status_code = status_code
        self._json_value = json_value

    def json(self):
        return self._json_value


def test_receive_control_updates_survives_null_payload(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _RawJsonResponse(200, None)

    result = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    assert result == 10
    store.close()


def test_receive_control_updates_survives_empty_list_result(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _RawJsonResponse(200, {"result": []})

    result = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    assert result == 10
    store.close()


def test_receive_control_updates_survives_null_entry_in_result(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _RawJsonResponse(200, {"result": [None]})

    result = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    assert result == 10
    assert query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED]) == []
    store.close()


def test_receive_control_updates_survives_null_chat_and_advances_cursor(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _RawJsonResponse(
            200, {"result": [{"update_id": 11, "message": {"chat": None}}]}
        )

    result = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    # A malformed update must not emit an event, but the cursor still
    # advances past it (the update_id is trustworthy even if its
    # payload isn't) so the next poll doesn't refetch it forever.
    assert result == 11
    assert query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED]) == []
    store.close()


@pytest.mark.parametrize("bad_update_id", ["11", {}, [], 1.5, True])
def test_receive_control_updates_ignores_non_int_update_id_and_next_poll_is_safe(tmp_path, bad_update_id):
    # A malformed update_id used to be adopted as the new cursor, then
    # crash `last_update_id + 1` on the NEXT poll - well after this call
    # already returned "successfully" (Review Task #70, 2nd
    # revalidation). It must instead keep the last known-safe cursor.
    store = Store(tmp_path / "state.db")
    seen_params = []

    def fake_get_first(url, params, timeout):
        seen_params.append(params)
        return _RawJsonResponse(
            200, {"result": [{"update_id": bad_update_id, "message": {"chat": None}}]}
        )

    first = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get_first, last_update_id=10)
    assert first == 10

    def fake_get_second(url, params, timeout):
        seen_params.append(params)
        return _RawJsonResponse(200, {"result": []})

    second = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get_second, last_update_id=first)
    assert second == 10
    assert seen_params[1]["offset"] == 11
    store.close()


@pytest.mark.parametrize("regressed_update_id", [9, 10])
def test_receive_control_updates_rejects_cursor_regression(tmp_path, regressed_update_id):
    # Review Task #70, 3rd revalidation: an update_id at or below the
    # already-accepted cursor (old, duplicate, or replayed) must not move
    # the cursor backward and must not be delivered as a "new" command.
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _RawJsonResponse(
            200,
            {"result": [{
                "update_id": regressed_update_id,
                "message": {"chat": {"id": 111}, "text": "Approve"},
            }]},
        )

    result = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    assert result == 10
    assert query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED]) == []
    store.close()


@pytest.mark.parametrize("bad_update_id", ["11", {}, [], 1.5])
def test_receive_control_updates_never_emits_for_invalid_update_id_even_with_valid_message(tmp_path, bad_update_id):
    # An invalid update_id used to still let a valid chat/text through to
    # emit() - only the cursor assignment was skipped, not the whole
    # update. Two identical polls with the same malformed id must not
    # deliver the same command twice (Review Task #70, 3rd revalidation).
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _RawJsonResponse(
            200,
            {"result": [{
                "update_id": bad_update_id,
                "message": {"chat": {"id": 111}, "text": "Approve"},
            }]},
        )

    first = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=10)
    second = receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, last_update_id=first)
    assert first == 10 and second == 10
    assert query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED]) == []
    store.close()


def test_send_message_error_never_leaks_url_or_token():
    import requests

    def fake_post(url, json, timeout):
        raise requests.exceptions.InvalidURL(f"Invalid URL: {url}")

    ok, error = send_control_message("oi", config=_CONFIGURED, post_fn=fake_post)
    assert ok is False
    assert "fake-token" not in error
    assert "http" not in error.lower()


# --- issue #148: voice messages in the control channel -----------------------

def test_voice_message_is_transcribed_and_emitted_as_text(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [
                {"update_id": 9001, "message": {"chat": {"id": 111}, "voice": {"file_id": "voice-abc"}}}
            ]
        })

    seen = []

    def fake_transcribe(file_id, config, get_fn):
        seen.append(file_id)
        return "Objetivo: testar por voz"

    receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, transcribe_fn=fake_transcribe)

    assert seen == ["voice-abc"]
    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert events[0]["payload"]["text"] == "Objetivo: testar por voz"
    assert events[0]["payload"]["transcribed"] is True
    store.close()


def test_audio_message_also_transcribed_like_voice(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "audio": {"file_id": "audio-xyz"}}}]
        })

    receive_control_updates(
        store, config=_CONFIGURED, get_fn=fake_get,
        transcribe_fn=lambda file_id, config, get_fn: f"transcrito:{file_id}",
    )

    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert events[0]["payload"]["text"] == "transcrito:audio-xyz"
    store.close()


def test_text_message_never_triggers_transcription(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "Objetivo: X",
                                                     "voice": {"file_id": "should-be-ignored"}}}]
        })

    def fail_transcribe(*a, **k):
        raise AssertionError("must not be called when text is already present")

    receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, transcribe_fn=fail_transcribe)

    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert events[0]["payload"]["text"] == "Objetivo: X"
    assert events[0]["payload"]["transcribed"] is False
    store.close()


def test_voice_message_transcription_failure_emits_empty_text_not_a_crash(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "voice": {"file_id": "bad-audio"}}}]
        })

    new_offset = receive_control_updates(
        store, config=_CONFIGURED, get_fn=fake_get,
        transcribe_fn=lambda file_id, config, get_fn: None,
    )

    assert new_offset == 1
    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert events[0]["payload"]["text"] == ""
    assert events[0]["payload"]["transcribed"] is False
    store.close()


def test_voice_message_without_file_id_is_ignored_safely(tmp_path):
    store = Store(tmp_path / "state.db")

    def fake_get(url, params, timeout):
        return _FakeResponse(200, {
            "result": [{"update_id": 1, "message": {"chat": {"id": 111}, "voice": {}}}]
        })

    def fail_transcribe(*a, **k):
        raise AssertionError("must not be called without a file_id")

    receive_control_updates(store, config=_CONFIGURED, get_fn=fake_get, transcribe_fn=fail_transcribe)

    events = query_events(store, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    assert events[0]["payload"]["text"] == ""
    store.close()


def test_download_telegram_file_happy_path_returns_bytes():
    from orchestrator.telegram_bot import _download_telegram_file

    calls = []

    def fake_get(url, timeout, params=None):
        calls.append(url)
        if "getFile" in url:
            return _FakeResponse(200, {"result": {"file_path": "voice/file_1.oga"}})
        return _FakeResponse(200, content=b"fake-ogg-bytes")

    result = _download_telegram_file("voice-abc", _CONFIGURED, fake_get)
    assert result == b"fake-ogg-bytes"
    assert any("getFile" in c for c in calls)
    assert any("file/botfake-token/voice/file_1.oga" in c for c in calls)


def test_download_telegram_file_returns_none_when_get_file_fails():
    from orchestrator.telegram_bot import _download_telegram_file

    def fake_get(url, timeout, params=None):
        return _FakeResponse(404)

    assert _download_telegram_file("voice-abc", _CONFIGURED, fake_get) is None


def test_download_telegram_file_returns_none_on_missing_file_path():
    from orchestrator.telegram_bot import _download_telegram_file

    def fake_get(url, timeout, params=None):
        return _FakeResponse(200, {"result": {}})

    assert _download_telegram_file("voice-abc", _CONFIGURED, fake_get) is None


def test_download_telegram_file_never_raises_on_network_exception():
    from orchestrator.telegram_bot import _download_telegram_file

    def fake_get(url, timeout, params=None):
        raise ConnectionError("offline")

    assert _download_telegram_file("voice-abc", _CONFIGURED, fake_get) is None


def test_transcribe_telegram_voice_without_groq_key_returns_none_without_downloading():
    from orchestrator.telegram_bot import _transcribe_telegram_voice

    def fail_get(*a, **k):
        raise AssertionError("must not attempt download without a Groq API key")

    cfg = OrchestratorConfig(telegram_bot_token="fake-token", groq_api_key=None)
    assert _transcribe_telegram_voice("voice-abc", cfg, fail_get) is None


def test_transcribe_telegram_voice_calls_groq_with_downloaded_audio():
    # groq_client_factory injection means this never needs the real `groq`
    # package importable - this package's own test/CI env deliberately
    # doesn't carry it (only main.py's runtime venv does).
    from orchestrator.telegram_bot import _transcribe_telegram_voice

    def fake_get(url, timeout, params=None):
        if "getFile" in url:
            return _FakeResponse(200, {"result": {"file_path": "voice/file_1.oga"}})
        return _FakeResponse(200, content=b"real-audio-bytes")

    captured = {}

    class _FakeTranscriptions:
        def create(self, *, file, model, language, response_format):
            captured["file"] = file
            captured["model"] = model
            captured["language"] = language
            return "Objetivo: transcrito de verdade"

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeGroqClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.audio = _FakeAudio()

    cfg = OrchestratorConfig(telegram_bot_token="fake-token", groq_api_key="fake-groq-key")
    result = _transcribe_telegram_voice("voice-abc", cfg, fake_get, groq_client_factory=_FakeGroqClient)

    assert result == "Objetivo: transcrito de verdade"
    assert captured["api_key"] == "fake-groq-key"
    assert captured["model"] == "whisper-large-v3-turbo"
    assert captured["language"] == "pt"
    assert captured["file"][1] == b"real-audio-bytes"


def test_transcribe_telegram_voice_returns_none_when_groq_raises():
    from orchestrator.telegram_bot import _transcribe_telegram_voice

    def fake_get(url, timeout, params=None):
        if "getFile" in url:
            return _FakeResponse(200, {"result": {"file_path": "voice/file_1.oga"}})
        return _FakeResponse(200, content=b"real-audio-bytes")

    class _FakeGroqClient:
        def __init__(self, api_key):
            pass

        class audio:
            class transcriptions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("groq is down")

    cfg = OrchestratorConfig(telegram_bot_token="fake-token", groq_api_key="fake-groq-key")
    assert _transcribe_telegram_voice("voice-abc", cfg, fake_get, groq_client_factory=_FakeGroqClient) is None


def test_transcribe_telegram_voice_returns_none_on_empty_transcription():
    from orchestrator.telegram_bot import _transcribe_telegram_voice

    def fake_get(url, timeout, params=None):
        if "getFile" in url:
            return _FakeResponse(200, {"result": {"file_path": "voice/file_1.oga"}})
        return _FakeResponse(200, content=b"real-audio-bytes")

    class _FakeGroqClient:
        def __init__(self, api_key):
            self.audio = self

        class transcriptions:
            @staticmethod
            def create(**kwargs):
                return "   "

    cfg = OrchestratorConfig(telegram_bot_token="fake-token", groq_api_key="fake-groq-key")
    assert _transcribe_telegram_voice("voice-abc", cfg, fake_get, groq_client_factory=_FakeGroqClient) is None
