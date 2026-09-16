"""Tests for orchestrator.conversation (issue #149)."""
import pytest

import orchestrator.conversation as conversation
from orchestrator.config import OrchestratorConfig
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _no_real_paperclip(monkeypatch):
    monkeypatch.setattr(
        conversation.paperclip_client, "get_snapshot",
        lambda: {"available": False, "reason": "not mocked in this test"},
    )


def test_chat_fn_receives_text_and_real_snapshot_context(monkeypatch, store):
    monkeypatch.setattr(
        conversation.paperclip_client, "get_snapshot",
        lambda: {
            "available": True,
            "companies": [{"name": "Argos", "agents": [{"name": "Onboarding", "status": "working"}],
                          "issues_by_status": {"open": 2}, "open_issues": []}],
        },
    )
    store.save_task(Task(title="X", objective="x", state=TaskState.IN_PROGRESS))

    seen = {}
    def fake_chat(text, context):
        seen["text"] = text
        seen["context"] = context
        return "Resposta sintetizada."

    result = conversation.answer_free_text("Quais projetos existem?", store=store, chat_fn=fake_chat)

    assert result == "Resposta sintetizada."
    assert seen["text"] == "Quais projetos existem?"
    assert "Argos" in seen["context"]
    assert "Onboarding" in seen["context"]
    assert "open=2" in seen["context"]
    assert "IN_PROGRESS" in seen["context"]


def test_paperclip_unavailable_is_stated_plainly_in_context_not_hidden(monkeypatch):
    monkeypatch.setattr(
        conversation.paperclip_client, "get_snapshot",
        lambda: {"available": False, "reason": "offline"},
    )
    seen = {}
    def fake_chat(text, context):
        seen["context"] = context
        return "ok"

    conversation.answer_free_text("oi", chat_fn=fake_chat)
    assert "indisponivel" in seen["context"].lower()
    assert "offline" in seen["context"]


def test_chat_fn_returning_none_falls_back_to_generic_no_answer_message():
    result = conversation.answer_free_text("oi", chat_fn=lambda text, context: None)
    assert result == conversation._NO_ANSWER


def test_chat_fn_raising_never_propagates():
    def boom(text, context):
        raise RuntimeError("network exploded")

    result = conversation.answer_free_text("oi", chat_fn=boom)
    assert result == conversation._UNAVAILABLE


def test_without_groq_key_and_no_chat_fn_returns_honest_message():
    cfg = OrchestratorConfig(groq_api_key=None)
    result = conversation.answer_free_text("oi", config=cfg)
    assert result == conversation._NO_KEY


def test_no_store_omits_store_section_without_crashing(monkeypatch):
    seen = {}
    def fake_chat(text, context):
        seen["context"] = context
        return "ok"

    conversation.answer_free_text("oi", store=None, chat_fn=fake_chat)
    assert "Orquestrador Jarvis" not in seen["context"]


def test_real_groq_client_is_used_when_no_chat_fn_and_key_present(monkeypatch):
    captured = {}

    class _FakeMessage:
        content = "Resposta real via Groq."

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeCompletions:
        def create(self, *, model, messages, max_tokens, temperature):
            captured["model"] = model
            captured["messages"] = messages
            class _R:
                choices = [_FakeChoice()]
            return _R()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeGroqClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.chat = _FakeChat()

    import sys
    fake_groq_module = type(sys)("groq")
    fake_groq_module.Groq = _FakeGroqClient
    monkeypatch.setitem(sys.modules, "groq", fake_groq_module)

    cfg = OrchestratorConfig(groq_api_key="fake-key")
    result = conversation.answer_free_text("oi", config=cfg)

    assert result == "Resposta real via Groq."
    assert captured["api_key"] == "fake-key"
    assert captured["model"] == "openai/gpt-oss-120b"
