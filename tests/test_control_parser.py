"""Control routing with the real planner pipeline and persisted decisions (#31)."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import sqlite3

import pytest

import orchestrator.decisions as decisions
from orchestrator.decisions import format_decision_message, get_priority_queue, handle_control_message
from orchestrator.events import EventType, query_events
from orchestrator.models import AgentName, Task, TaskState
from orchestrator.persistence import Store
from orchestrator.planner import plan
from orchestrator.project_resolver import ProjectContext


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'state.db')
    yield value
    value.close()


def no_planner(*args, **kwargs):
    pytest.fail('This control message must not invoke planning')


def route(store, text, **kwargs):
    return handle_control_message(text, store=store, plan_fn=kwargs.pop('plan_fn', no_planner),
                                  send_fn=kwargs.pop('send_fn', lambda text: (True, None)), **kwargs)


def pending(store, task=None, *, correlation='decision-1', problem='Choose approach'):
    task = task or Task(title='Change approach', objective='Implement feature',
                        state=TaskState.NEEDS_LUCAS, preferred_agent=AgentName.CODEX,
                        project_id='argos')
    store.save_task(task)
    message = format_decision_message(task, problem, 'Tradeoff', ['Keep', 'Change'], 'Keep', 'No cost')
    store.save_decision(correlation, message, task.id)
    return task, message.splitlines()[0].split()[-1]


def test_new_objective_uses_real_three_stage_planner(store):
    class Resolver:
        def resolve_from_text(self, text):
            return ProjectContext(canonical_id='argos', root='', primary_context=None, always_read=[])
    prompts = []
    outputs = iter(['Plan', 'Critique', json.dumps([
        {'title': 'Feature', 'objective': 'Build feature', 'depends_on_index': []}])])
    def llm(prompt):
        prompts.append(prompt)
        return next(outputs)
    result = route(store, 'Quero que o Argos crie uma tela de status',
                   plan_fn=partial(plan, resolver=Resolver(), llm_generate=llm))
    assert result.kind == 'plan' and result.delivered
    assert result.plan.project_id == 'argos'
    assert len(result.plan.tasks) == 1 and len(prompts) == 3
    assert result.plan.tasks[0].state == TaskState.PLANNED
    assert 'EXTERNAL_CONTENT' in prompts[0]
    assert store.list_tasks() == []  # #23 owns publishing/persisting the plan


def test_unrecognized_text_falls_back_to_free_conversation_when_wired(store):
    # Issue #149: text matching NONE of the structured shapes (Objetivo/
    # REF/Prioriza) answers via fallback_fn instead of a bare "nao entendi".
    seen = []
    def fallback(text):
        seen.append(text)
        return "Resposta livre baseada em dados reais."

    result = route(store, 'Quais projetos voce consegue mexer agora?', fallback_fn=fallback)

    assert seen == ['Quais projetos voce consegue mexer agora?']
    assert result.kind == 'conversation'
    assert result.message == "Resposta livre baseada em dados reais."


def test_without_fallback_fn_unrecognized_text_still_asks_for_clarification(store):
    # No regression: omitting fallback_fn (the default) keeps the original
    # behavior exactly as it was before #149.
    result = route(store, 'Quais projetos voce consegue mexer agora?')
    assert result.kind == 'clarification'
    assert 'Objetivo:' in result.message


def test_fallback_fn_returning_empty_string_still_asks_for_clarification(store):
    result = route(store, 'oi tudo bem?', fallback_fn=lambda text: '')
    assert result.kind == 'clarification'


@pytest.mark.parametrize('text', [
    'REF: xyz sim',            # explicit reply grammar, ambiguous/no match
    'sim',                      # bare option/yes-no with no pending decision
    'Objetivo:',                # recognized as objective, but empty content
    'Prioriza tarefa-123',      # priority command, has its own real handler
])
def test_fallback_fn_never_called_for_recognized_structured_shapes(store, text):
    def fail_fallback(text):
        pytest.fail('fallback_fn must not run for a recognized structured command')

    route(store, text, fallback_fn=fail_fallback)


def test_objective_without_colon_is_accepted(store):
    # Issue #148: a voice message transcribed via Groq Whisper naturally
    # drops punctuation ("Objetivo testar X" not "Objetivo: testar X").
    # Every other trigger verb here ("quero que", "cria", ...) already
    # needs no colon at all - "objetivo" alone must be consistent with
    # them, or a genuine spoken command from #148 is silently rejected.
    calls = []
    from orchestrator.planner import PlanResult
    def planner(objective, **kwargs):
        calls.append(objective)
        return PlanResult()
    assert route(store, 'Objetivo testar transcricao de voz', plan_fn=planner).kind == 'plan'
    assert calls == ['testar transcricao de voz']


def test_explicit_new_objective_is_not_consumed_by_pending_decision(store):
    pending(store)
    calls = []
    from orchestrator.planner import PlanResult
    def planner(objective, **kwargs):
        calls.append(objective)
        return PlanResult()
    assert route(store, 'Objetivo: criar testes do Argos', plan_fn=planner).kind == 'plan'
    assert calls == ['criar testes do Argos']
    assert len(store.get_pending_decisions()) == 1


@pytest.mark.parametrize('text', ['B', 'opção B', 'Pode usar a opção B', 'Sim', 'Não'])
def test_unique_answer_readmits_exact_task_with_response_and_agent(store, text):
    task, _ = pending(store)
    independent = Task(title='Independent', objective='Work', state=TaskState.READY)
    store.save_task(independent)
    result = route(store, text)
    assert result.kind == 'decision' and result.task_id == task.id
    resumed = store.get_task(task.id)
    assert resumed.state == TaskState.PLANNED
    assert resumed.preferred_agent == AgentName.CODEX
    assert text in resumed.context
    assert store.get_task(independent.id).to_dict() == independent.to_dict()
    events = query_events(store, event_types=[EventType.DECISION_RECEIVED])
    assert len(events) == 1 and events[0]['correlation_id'] == 'decision-1'
    assert events[0]['payload']['response'] == text
    assert events[0]['payload']['resume_requested']


def test_multiple_pending_need_ref_and_resolve_only_selected_task(store):
    one, ref_one = pending(store)
    two, ref_two = pending(store, correlation='decision-2')
    assert route(store, 'B').kind == 'clarification'
    assert len(store.get_pending_decisions()) == 2
    assert route(store, f'REF: {ref_two} B').task_id == two.id
    assert store.get_task(one.id).state == TaskState.NEEDS_LUCAS
    assert store.get_task(two.id).state == TaskState.PLANNED
    assert route(store, f'REF: {ref_two} B').kind == 'clarification'
    assert len(query_events(store, event_types=[EventType.DECISION_RECEIVED])) == 1


def test_two_questions_on_one_task_require_last_answer_before_readmission(store):
    task, ref_one = pending(store)
    _, ref_two = pending(store, task, correlation='decision-2', problem='Another choice')
    assert route(store, f'Resposta {task.id} B').kind == 'clarification'
    assert route(store, f'REF: {ref_one} A').kind == 'decision'
    assert store.get_task(task.id).state == TaskState.NEEDS_LUCAS
    assert route(store, f'REF: {ref_two} B').kind == 'decision'
    restored = store.get_task(task.id)
    assert restored.state == TaskState.PLANNED
    assert 'decision-1' in restored.context and 'decision-2' in restored.context


def test_reused_ref_is_ambiguous_even_if_hash_collides(store):
    task, ref = pending(store)
    message = store.get_pending_decisions()[0]['message']
    store.save_decision('different-question', message, task.id)
    assert route(store, f'REF: {ref} B').kind == 'clarification'
    assert len(store.get_pending_decisions()) == 2


def test_exact_correlation_accepts_free_text_without_executing_it(store):
    task, _ = pending(store)
    response = 'Nao altere billing. Mantenha o plano atual.'
    assert route(store, f'Resposta decision-1 {response}').kind == 'decision'
    assert response in store.get_task(task.id).context
    assert store.query('SELECT response FROM decisions')[0][0] == response


def test_nonexistent_option_leaves_decision_pending(store):
    pending(store)
    assert route(store, 'opcao J').kind == 'clarification'
    assert len(store.get_pending_decisions()) == 1


@pytest.mark.parametrize('state', [TaskState.BLOCKED, TaskState.IN_PROGRESS, TaskState.IN_REVIEW])
def test_answer_does_not_erase_other_block_or_change_active_task_state(store, state):
    task, _ = pending(store, Task(title='Work', objective='Work', state=state))
    assert route(store, 'B').kind == 'decision'
    assert store.get_task(task.id).state == state
    assert not query_events(store, event_types=[EventType.DECISION_RECEIVED])[0]['payload']['resume_requested']


@pytest.mark.parametrize('state', [TaskState.DONE, TaskState.FAILED])
def test_terminal_task_not_reopened_by_stale_decision(store, state):
    pending(store, Task(title='Work', objective='Work', state=state))
    assert route(store, 'B').kind == 'clarification'
    assert len(store.get_pending_decisions()) == 1


def test_missing_task_does_not_consume_answer(store):
    store.save_decision('missing', 'Question', 'no-task')
    assert route(store, 'Sim').kind == 'clarification'
    assert len(store.get_pending_decisions()) == 1


def test_prioritization_changes_eligible_order_without_replanning(store):
    first = Task(title='First', objective='First', state=TaskState.READY)
    second = Task(title='Second', objective='Second', state=TaskState.READY)
    store.save_task(first)
    store.save_task(second)
    assert get_priority_queue(store)[0].id == first.id
    assert route(store, f'Prioriza a tarefa {second.id}').kind == 'priority'
    assert get_priority_queue(store)[0].id == second.id
    assert store.get_task(first.id).priority == 'medium'


def test_priority_by_exact_accented_title_keeps_dependencies_and_blockers(store):
    task = Task(title='Correção de login', objective='Fix', state=TaskState.PLANNED, dependencies=['missing'])
    store.save_task(task)
    assert route(store, 'Prioriza Correcao de login').kind == 'priority'
    actual = store.get_task(task.id)
    assert actual.priority == 'urgent' and actual.dependencies == ['missing']
    assert actual.state == TaskState.PLANNED and get_priority_queue(store) == []


def test_ambiguous_title_does_not_prioritize_arbitrary_task(store):
    for _ in range(2):
        store.save_task(Task(title='Same title', objective='Work', state=TaskState.READY))
    assert route(store, 'Prioriza Same title').kind == 'clarification'
    assert all(task.priority == 'medium' for task in store.list_tasks())


@pytest.mark.parametrize('state', [TaskState.IN_PROGRESS, TaskState.IN_REVIEW, TaskState.DONE, TaskState.FAILED])
def test_priority_does_not_modify_started_or_terminal_work(store, state):
    task = Task(title='Work', objective='Work', state=state)
    store.save_task(task)
    assert route(store, f'Prioriza {task.id}').kind == 'clarification'
    assert store.get_task(task.id).to_dict() == task.to_dict()


@pytest.mark.parametrize('text', ['', None, 'Talvez depois', 'Quero a opcao B', 'Nao priorize aquela tarefa', 'REF: unknown B'])
def test_ambiguous_or_invalid_text_has_no_side_effects(store, text):
    pending(store)
    assert route(store, text).kind == 'clarification'
    assert len(store.get_pending_decisions()) == 1


def test_event_failure_rolls_back_answer_and_task_together(store, monkeypatch):
    task, _ = pending(store)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic secret in failure')
    monkeypatch.setattr(decisions, 'emit_in_transaction', fail)
    result = route(store, 'B')
    assert result.kind == 'error' and 'secret' not in result.message
    assert len(store.get_pending_decisions()) == 1
    assert store.get_task(task.id).to_dict() == task.to_dict()


def test_send_failure_does_not_undo_or_repeat_committed_answer(store):
    task, ref = pending(store)
    result = route(store, f'REF: {ref} B', send_fn=lambda _: (False, 'offline'))
    assert result.kind == 'decision' and not result.delivered
    assert store.get_task(task.id).state == TaskState.PLANNED
    assert route(store, f'REF: {ref} B').kind == 'clarification'
    assert len(query_events(store, event_types=[EventType.DECISION_RECEIVED])) == 1


def test_concurrent_answers_from_separate_connections_commit_only_once(tmp_path):
    path = tmp_path / 'shared.db'
    store = Store(path)
    task, ref = pending(store)
    def answer(_):
        connection = Store(path)
        try:
            return route(connection, f'REF: {ref} B').kind
        finally:
            connection.close()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(answer, range(8)))
        assert results.count('decision') == 1
        assert len(query_events(store, event_types=[EventType.DECISION_RECEIVED])) == 1
    finally:
        store.close()


def test_planner_failure_is_reported_without_exposing_provider_details(store):
    def fail(*args, **kwargs):
        raise RuntimeError('token=synthetic-secret')
    result = route(store, 'Objetivo: criar uma tela', plan_fn=fail)
    assert result.kind == 'error' and 'synthetic-secret' not in result.message


def test_human_gated_goal_keeps_real_planner_gate(store):
    result = route(store, 'Objetivo: Mudar billing do Argos para Pro', plan_fn=plan)
    assert result.kind == 'plan' and result.plan.needs_human_decision
    assert result.plan.tasks == []
