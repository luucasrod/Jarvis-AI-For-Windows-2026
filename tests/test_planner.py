"""Tests for orchestrator.planner (issue #22).

All LLM calls are injected fakes - no real Gemini/Groq call happens in
this suite (see the module docstring in orchestrator/planner.py for why).
"""
import json

import pytest

from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult, plan
from orchestrator.project_resolver import ProjectContext, ResolveError


class _FakeResolver:
    """Minimal stand-in for ProjectResolver, isolated from #15's own
    index-parsing tests - this suite only cares that planner.plan() reacts
    correctly to whatever the resolver returns."""

    def __init__(self, result):
        self._result = result

    def resolve_from_text(self, text):
        return self._result


def _fake_llm_sequence(*responses):
    """Returns an llm_generate that yields `responses` in order, one per
    call, so tests can assert exactly what each of the 3 LLM calls
    (plan/critique/decompose) receives."""
    calls = {"count": 0}

    def _generate(prompt: str) -> str:
        idx = calls["count"]
        calls["count"] += 1
        return responses[idx] if idx < len(responses) else responses[-1]

    _generate.calls = calls
    return _generate


_SAMPLE_PROJECT = ProjectContext(
    canonical_id="cashy",
    root="A:\\Cashy-Native",
    primary_context=None,
    always_read=[],
)


def test_needs_human_objective_short_circuits_without_llm():
    def _should_not_be_called(prompt):
        raise AssertionError("LLM nao deveria ser chamado para objetivo NEEDS_LUCAS")

    result = plan(
        "Muda o plano de billing do Cashy pra Pro",
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=_should_not_be_called,
    )
    assert result.needs_human_decision is True
    assert result.decision_reason is not None
    assert result.tasks == []


def test_simple_objective_produces_at_least_one_task():
    decompose_json = json.dumps([
        {"title": "Criar tela de X", "objective": "Implementar tela X no Cashy",
         "acceptance_criteria": ["tela abre", "dados carregam"], "depends_on_index": [], "risk": "low"}
    ])
    llm = _fake_llm_sequence("plano inicial", "critica", decompose_json)

    result = plan(
        "cria uma tela de X no Cashy",
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=llm,
    )

    assert result.needs_human_decision is False
    assert result.project_id == "cashy"
    assert len(result.tasks) == 1
    assert result.tasks[0].title == "Criar tela de X"
    assert result.tasks[0].project_id == "cashy"
    assert llm.calls["count"] == 3  # plan + critique + decompose


def test_objective_with_internal_dependencies():
    decompose_json = json.dumps([
        {"title": "A - schema", "objective": "criar schema", "depends_on_index": [], "risk": "low"},
        {"title": "B - endpoint", "objective": "criar endpoint que usa o schema", "depends_on_index": [0], "risk": "medium"},
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan("implementa endpoint com schema novo", resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)

    assert len(result.tasks) == 2
    task_a, task_b = result.tasks
    assert task_b.dependencies == [task_a.id]
    assert task_a.dependencies == []


def test_malformed_decomposition_json_falls_back_to_single_task():
    llm = _fake_llm_sequence("plano", "critica", "isto nao e json valido {{{")

    result = plan("objetivo qualquer", resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)

    assert len(result.tasks) == 1
    assert result.tasks[0].objective == "objetivo qualquer"
    assert result.tasks[0].project_id == "cashy"


def test_decomposition_returning_non_list_json_falls_back():
    llm = _fake_llm_sequence("plano", "critica", json.dumps({"not": "a list"}))
    result = plan("objetivo qualquer", resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)
    assert len(result.tasks) == 1


def test_unresolved_project_still_produces_a_plan():
    llm = _fake_llm_sequence("plano generico", "critica", json.dumps([
        {"title": "Fazer algo", "objective": "obj", "depends_on_index": [], "risk": "low"}
    ]))
    result = plan(
        "faz uma coisa generica sem mencionar nenhum projeto conhecido",
        resolver=_FakeResolver(ResolveError(reason="nenhum projeto mencionado")),
        llm_generate=llm,
    )
    assert result.project_id is None
    assert result.resolver_error is not None
    assert len(result.tasks) == 1


def test_duplicate_objective_reuses_existing_task(tmp_path):
    store = Store(tmp_path / "state.db")
    existing = Task(title="Ja existe", objective="cria uma tela de X no Cashy", project_id="cashy", state=TaskState.READY)
    store.save_task(existing)

    def _should_not_be_called(prompt):
        raise AssertionError("LLM nao deveria ser chamado quando ha duplicata")

    result = plan(
        "cria uma tela de X no Cashy",
        store=store,
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=_should_not_be_called,
    )

    assert result.duplicate_of == existing.id
    assert result.tasks == [existing]
    store.close()


def test_done_duplicate_does_not_block_replanning(tmp_path):
    store = Store(tmp_path / "state.db")
    old = Task(title="Antiga", objective="cria uma tela de X no Cashy", project_id="cashy", state=TaskState.DONE)
    store.save_task(old)

    decompose_json = json.dumps([{"title": "Nova", "objective": "cria uma tela de X no Cashy", "depends_on_index": [], "risk": "low"}])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan("cria uma tela de X no Cashy", store=store, resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)

    assert result.duplicate_of is None
    assert result.tasks[0].title == "Nova"
    store.close()
