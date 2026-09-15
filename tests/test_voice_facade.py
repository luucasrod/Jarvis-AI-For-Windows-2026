"""Tests for orchestrator.voice_facade (issue #35): real answers built
from #13 (persistence)/#14 (events, via #34's history) and #18
(paperclip_client) - every test here injects KNOWN mocked state and
asserts the spoken answer reflects exactly that state, never more.
"""
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator import voice_facade
from orchestrator.events import EventType, emit
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store
from orchestrator.project_resolver import ProjectContext, ResolveError


@pytest.fixture
def store(tmp_path, monkeypatch):
    instance = Store(tmp_path / "state.db")
    monkeypatch.setattr(voice_facade, "_get_store", lambda: instance)
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _no_real_paperclip(monkeypatch):
    """Every test controls paperclip_client.get_snapshot explicitly -
    never let a test accidentally hit a real/absent Paperclip server."""
    monkeypatch.setattr(
        voice_facade.paperclip_client, "get_snapshot",
        lambda: {"available": False, "reason": "not mocked in this test"},
    )


def _snapshot(**companies_by_name):
    return {
        "available": True,
        "companies": [
            {"name": name, "agents": agents, "issues_by_status": issues, "open_issues": []}
            for name, (agents, issues) in companies_by_name.items()
        ],
    }


# --- handle_status_query: "Tem algo bloqueado?" ------------------------------

def test_blocked_query_reports_real_blocked_tasks(store):
    store.save_task(Task(title="Corrigir pipeline", objective="x", state=TaskState.BLOCKED))
    store.save_task(Task(title="Revisar README do Argos", objective="x", state=TaskState.BLOCKED))
    store.save_task(Task(title="Outra coisa", objective="x", state=TaskState.IN_PROGRESS))

    reply = voice_facade.handle_status_query("tem algo bloqueado?")

    assert "2" in reply
    assert "Corrigir pipeline" in reply
    assert "Revisar README do Argos" in reply
    assert "Outra coisa" not in reply


def test_blocked_query_is_honest_when_nothing_is_blocked(store):
    store.save_task(Task(title="Tarefa livre", objective="x", state=TaskState.IN_PROGRESS))
    reply = voice_facade.handle_status_query("tem algo bloqueado?")
    assert "nada bloqueado" in reply.lower()


# --- handle_status_query: "O que precisa de mim?" ----------------------------

def test_needs_me_query_reports_pending_decisions(store):
    task = Task(title="Deploy falhou", objective="x", state=TaskState.NEEDS_LUCAS)
    store.save_task(task)
    store.save_decision(correlation_id="ref-1", task_id=task.id, message="Reverter o deploy ou investigar?")

    reply = voice_facade.handle_status_query("o que precisa de mim?")

    assert "1" in reply
    assert "Reverter o deploy ou investigar" in reply


def test_needs_me_query_is_honest_when_nothing_pending(store):
    reply = voice_facade.handle_status_query("o que precisa de mim?")
    assert "nada" in reply.lower()


def test_needs_me_query_reports_both_a_pending_decision_and_a_needs_lucas_task_with_no_matching_decision(store):
    # Independent-review finding: decisions.py/review_pipeline.py can set
    # NEEDS_LUCAS on a task with no matching save_decision call for it -
    # a pending decision for one task must never silently hide an
    # UNRELATED task also waiting on Lucas.
    decided_task = Task(title="Deploy falhou", objective="x", state=TaskState.NEEDS_LUCAS)
    undecided_task = Task(title="Merge ambiguo", objective="x", state=TaskState.NEEDS_LUCAS)
    store.save_task(decided_task)
    store.save_task(undecided_task)
    store.save_decision(correlation_id="ref-1", task_id=decided_task.id, message="Reverter ou investigar?")

    reply = voice_facade.handle_status_query("o que precisa de mim?")

    assert "1 pergunta" in reply
    assert "2 tarefa" in reply


# --- handle_status_query: "Quem está trabalhando?" ---------------------------

def test_who_is_working_reports_real_agent_status(store, monkeypatch):
    monkeypatch.setattr(
        voice_facade.paperclip_client, "get_snapshot",
        lambda: _snapshot(Argos=([
            {"name": "Onboarding", "status": "working", "role": "dev"},
            {"name": "QA", "status": "paused", "pause_reason": "budget", "role": "qa"},
        ], {})),
    )
    reply = voice_facade.handle_status_query("quem esta trabalhando?")
    assert "Onboarding" in reply and "working" in reply
    assert "QA" in reply and "pausado por budget" in reply


def test_who_is_working_is_honest_when_paperclip_unavailable(store, monkeypatch):
    monkeypatch.setattr(
        voice_facade.paperclip_client, "get_snapshot",
        lambda: {"available": False, "reason": "offline"},
    )
    reply = voice_facade.handle_status_query("quem esta trabalhando?")
    assert "offline" in reply.lower()


def test_who_is_working_is_honest_when_no_agents_configured(store, monkeypatch):
    monkeypatch.setattr(voice_facade.paperclip_client, "get_snapshot", lambda: {"available": True, "companies": []})
    reply = voice_facade.handle_status_query("quem esta trabalhando?")
    assert "nenhum agente" in reply.lower()


# --- handle_status_query: "Como está o <projeto>?" ---------------------------

def test_project_query_is_honest_when_nothing_is_known(store, monkeypatch):
    monkeypatch.setattr(
        voice_facade.ProjectResolver, "resolve_from_text",
        lambda self, text: ProjectContext(canonical_id="argos"),
    )
    monkeypatch.setattr(voice_facade.paperclip_client, "get_snapshot", lambda: {"available": False, "reason": "x"})

    reply = voice_facade.handle_status_query("como esta o Argos?")

    assert "argos" in reply.lower()
    assert "nada registrado" in reply.lower()


def test_project_query_reports_local_task_counts_and_paperclip_issues(store, monkeypatch):
    resolved = ProjectContext(canonical_id="argos")
    monkeypatch.setattr(voice_facade.ProjectResolver, "resolve_from_text", lambda self, text: resolved)
    monkeypatch.setattr(
        voice_facade.paperclip_client, "get_snapshot",
        lambda: _snapshot(Argos=([], {"open": 3, "done": 5})),
    )
    store.save_task(Task(title="A", objective="x", project_id="argos", state=TaskState.IN_PROGRESS))
    store.save_task(Task(title="B", objective="x", project_id="argos", state=TaskState.IN_PROGRESS))
    store.save_task(Task(title="C", objective="x", project_id="outro", state=TaskState.BLOCKED))

    reply = voice_facade.handle_status_query("como esta o Argos?")

    assert "2 em IN_PROGRESS" in reply
    assert "3 open" in reply
    assert "5 done" in reply


def test_project_query_never_confuses_a_near_homonym_paperclip_company(store, monkeypatch):
    # Independent-review finding: "argos" is a substring of "argos-hub" -
    # a substring match would silently attribute Argos-Hub's Paperclip
    # data to a query about the DIFFERENT project "argos" (and vice
    # versa), exactly the near-homonym trap project_resolver.py's own
    # docstring documents (Argos vs Argos-Hub, Cashy vs Cashy-Android).
    resolved = ProjectContext(canonical_id="argos")
    monkeypatch.setattr(voice_facade.ProjectResolver, "resolve_from_text", lambda self, text: resolved)
    monkeypatch.setattr(
        voice_facade.paperclip_client, "get_snapshot",
        lambda: {
            "available": True,
            "companies": [
                {"name": "Argos-Hub", "agents": [], "issues_by_status": {"open": 99}, "open_issues": []},
            ],
        },
    )

    reply = voice_facade.handle_status_query("como esta o Argos?")

    assert "99" not in reply
    assert "nada registrado" in reply.lower()


def test_unrecognized_status_query_returns_none(store, monkeypatch):
    monkeypatch.setattr(voice_facade.ProjectResolver, "resolve_from_text", lambda self, text: ResolveError(reason="nao encontrado"))
    assert voice_facade.handle_status_query("qual a previsao do tempo?") is None


# --- handle_report_query ------------------------------------------------------

def test_report_query_summarizes_todays_real_events(store):
    now = datetime.now(timezone.utc)
    emit(store, EventType.TASK_COMPLETED, project_id="argos", created_at=now)
    emit(store, EventType.BUG_FOUND, project_id="argos", created_at=now)
    emit(store, EventType.TASK_COMPLETED, project_id="argos", created_at=now - timedelta(days=3))

    reply = voice_facade.handle_report_query()

    assert isinstance(reply, str) and reply
    assert "1 concluida(s)" in reply


def test_report_query_is_honest_when_nothing_happened_today(store):
    reply = voice_facade.handle_report_query()
    assert "nada relevante" in reply.lower()


# --- handle_control_query -----------------------------------------------------

def test_control_query_is_honest_about_missing_pause_mechanism():
    reply = voice_facade.handle_control_query("pausar a orquestração")
    assert isinstance(reply, str) and reply
    assert "não tenho" in reply.lower() or "nao tenho" in reply.lower()


def test_unrecognized_control_query_returns_none():
    assert voice_facade.handle_control_query("qual o sentido da vida?") is None


# --- never raises --------------------------------------------------------------

def test_facade_never_raises_on_empty_input(store, monkeypatch):
    monkeypatch.setattr(voice_facade.ProjectResolver, "resolve_from_text", lambda self, text: ResolveError(reason="vazio"))
    voice_facade.handle_status_query("")
    voice_facade.handle_control_query("")
    voice_facade.handle_report_query()
