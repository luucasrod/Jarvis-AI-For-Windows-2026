"""Tests for orchestrator.planner (issue #22).

All LLM calls are injected fakes - no real Gemini/Groq call happens in
this suite (see the module docstring in orchestrator/planner.py for why).
"""
import json

import pytest

from orchestrator.models import ExecutionMode, Task, TaskState
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


# --- Regression tests for the 5 issues Codex found in Review Task #62 -----
# (CHANGES_REQUESTED on PR #61). Reproductions adapted directly from
# Codex's review comment on #62, credited there.

def test_original_dependency_indices_survive_filtering():
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition(
        json.dumps([None, {"title": "A"}, {"title": "B", "depends_on_index": [1]}]),
        "cashy",
    )
    assert len(tasks) == 2
    task_a, task_b = tasks
    assert task_b.dependencies == [task_a.id]


def test_wrong_type_for_whole_depends_on_index_field_blocks_task():
    # depends_on_index itself is a bare int, not a list - unlike an
    # omitted field, this IS a declared dependency we can't trust, so it
    # must not resolve as "no dependency" (review #62, 3rd pass).
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition('[{"title":"A","depends_on_index":1}]', "cashy")
    assert isinstance(tasks, list)
    assert tasks[0].dependencies == []
    assert tasks[0].state == TaskState.BLOCKED


def test_bool_in_depends_on_index_is_ignored_not_treated_as_int():
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition(
        json.dumps([{"title": "A"}, {"title": "B", "depends_on_index": [True, 0]}]),
        "cashy",
    )
    task_a, task_b = tasks
    assert task_b.dependencies == [task_a.id]


def test_solo_execution_mode_from_json_is_preserved():
    decompose_json = json.dumps([
        {"title": "Editar arquivo compartilhado", "objective": "mexe em main.py",
         "execution_mode": "SOLO", "risk": "high", "depends_on_index": []}
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan(
        "altera o arquivo compartilhado do Cashy em execucao solo por risco de conflito",
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=llm,
    )
    assert result.tasks[0].execution_mode == ExecutionMode.SOLO


def test_generated_task_requiring_human_decision_is_flagged_even_if_original_objective_was_innocuous():
    decompose_json = json.dumps([
        {"title": "Alterar billing", "objective": "Mudar billing para plano pago", "risk": "high", "depends_on_index": []}
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan("Melhore o Cashy", resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)

    assert result.needs_human_decision or all(t.state == TaskState.NEEDS_LUCAS for t in result.tasks)


def test_generated_task_dedup_against_existing_store_task(tmp_path):
    store = Store(tmp_path / "state.db")
    existing = Task(title="Schema", objective="Criar schema", project_id="cashy", state=TaskState.READY)
    store.save_task(existing)

    decompose_json = json.dumps([{"title": "Schema", "objective": "Criar schema", "depends_on_index": []}])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan(
        "Implemente endpoint e schema no Cashy",
        store=store,
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=llm,
    )

    assert not any(t.objective == existing.objective and t.id != existing.id for t in result.tasks)
    assert result.tasks[0].id == existing.id
    store.close()


def test_dedup_remaps_dependencies_to_existing_task_id(tmp_path):
    store = Store(tmp_path / "state.db")
    existing = Task(title="Schema", objective="Criar schema", project_id="cashy", state=TaskState.READY)
    store.save_task(existing)

    decompose_json = json.dumps([
        {"title": "Schema", "objective": "Criar schema", "depends_on_index": []},
        {"title": "Endpoint", "objective": "Criar endpoint novo", "depends_on_index": [0]},
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan(
        "Implemente endpoint e schema no Cashy",
        store=store,
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=llm,
    )

    schema_task = next(t for t in result.tasks if t.title == "Schema")
    endpoint_task = next(t for t in result.tasks if t.title == "Endpoint")
    assert schema_task.id == existing.id
    assert endpoint_task.dependencies == [existing.id]
    store.close()


# --- Regression tests from Codex's 2nd review pass on #62 (PR #61) ---------

def test_out_of_range_dependency_reference_blocks_task_instead_of_releasing_it():
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition(
        json.dumps([{"title": "Publish dependent result", "depends_on_index": [99]}]),
        "cashy",
    )
    assert len(tasks) == 1
    assert tasks[0].state == TaskState.BLOCKED


def test_dependency_pointing_at_filtered_invalid_item_blocks_task():
    from orchestrator.planner import _parse_decomposition

    # index 0 is an invalid item (no title) - a reference to it must not
    # silently resolve as "no dependency".
    tasks = _parse_decomposition(
        json.dumps([{"no_title_here": True}, {"title": "B", "depends_on_index": [0]}]),
        "cashy",
    )
    assert len(tasks) == 1
    assert tasks[0].state == TaskState.BLOCKED


def test_wrong_type_entry_in_depends_on_index_blocks_task():
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition(
        json.dumps([{"title": "A", "depends_on_index": ["not-an-int"]}]),
        "cashy",
    )
    assert tasks[0].state == TaskState.BLOCKED


def test_simple_cycle_between_two_tasks_is_flagged_blocked():
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition(
        json.dumps([
            {"title": "A", "depends_on_index": [1]},
            {"title": "B", "depends_on_index": [0]},
        ]),
        "cashy",
    )
    assert all(t.state == TaskState.BLOCKED for t in tasks)


def test_self_dependency_alone_is_a_cycle_of_size_one_and_blocks():
    # A task depending on itself can never become unblocked - it must be
    # flagged the same way a 2-node cycle is, not silently dropped as
    # redundant (review #62, 3rd pass).
    from orchestrator.planner import _parse_decomposition

    tasks = _parse_decomposition(
        json.dumps([{"title": "A", "depends_on_index": [0]}]),
        "cashy",
    )
    assert tasks[0].dependencies == []
    assert tasks[0].state == TaskState.BLOCKED


# --- Regression tests from Codex's 3rd review pass on #62 (PR #61) --------
# Same two gaps as above, reproduced through the public plan() entry point
# (not just the internal _parse_decomposition helper) per Codex's request.

def test_plan_blocks_task_when_depends_on_index_field_has_wrong_type():
    decompose_json = json.dumps([
        {"title": "Publish report", "objective": "publica", "depends_on_index": 1},
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan(
        "Gera e publica um relatorio no Cashy",
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=llm,
    )

    assert result.tasks[0].dependencies == []
    assert result.tasks[0].state == TaskState.BLOCKED


def test_plan_blocks_task_with_self_reference_in_depends_on_index():
    decompose_json = json.dumps([
        {"title": "Publish report", "objective": "publica", "depends_on_index": [0]},
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan(
        "Gera e publica um relatorio no Cashy",
        resolver=_FakeResolver(_SAMPLE_PROJECT),
        llm_generate=llm,
    )

    assert result.tasks[0].dependencies == []
    assert result.tasks[0].state == TaskState.BLOCKED


def test_flag_dependency_cycles_runs_again_after_dedup_style_remap():
    # Direct unit test of the helper _flag_dependency_cycles is called a
    # SECOND time (after _dedup_generated_tasks) in plan(), specifically
    # because remapping ids during dedup can introduce a cycle that
    # didn't exist in the raw decomposition (ids are randomly generated,
    # so this scenario can't be triggered deterministically through
    # plan() itself without controlling id generation - exercised
    # directly here instead).
    from orchestrator.planner import _flag_dependency_cycles

    task_x = Task(id="x", title="X", objective="obj", dependencies=["y"])
    task_y = Task(id="y", title="Y", objective="obj", dependencies=["x"])
    _flag_dependency_cycles([task_x, task_y])
    assert task_x.state == TaskState.BLOCKED
    assert task_y.state == TaskState.BLOCKED


def test_priority_from_decomposition_json_is_preserved():
    decompose_json = json.dumps([
        {"title": "Fix urgent outage", "objective": "obj", "priority": "high", "depends_on_index": []}
    ])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan("resolve a queda urgente do Cashy", resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)

    assert result.tasks[0].priority == "high"


def test_priority_defaults_to_medium_when_absent():
    decompose_json = json.dumps([{"title": "A", "objective": "obj", "depends_on_index": []}])
    llm = _fake_llm_sequence("plano", "critica", decompose_json)

    result = plan("objetivo qualquer", resolver=_FakeResolver(_SAMPLE_PROJECT), llm_generate=llm)

    assert result.tasks[0].priority == "medium"
