"""Human-decision (NEEDS_LUCAS) messaging and routing (issue #33).

Formats decision messages using the exact template from section 30, and
registers+sends them so Lucas can respond over the Telegram control
channel (#19). Also provides the resolution half: routing Lucas's
response back to the pending decision (and, transitively, the task it
blocks) so other independent tasks keep progressing while this one waits
(reinforces #21's/#20's continuity guarantee - a blocked task never
blocks unrelated ones).

`handle_control_message()` (#31) routes trusted control-channel input to
planning, pending decisions, or queue priority. It never starts an executor.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from orchestrator.events import EventType, emit_in_transaction
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult, plan
from orchestrator.task_queue import get_promotable_tasks
from orchestrator.telegram_bot import send_control_message

_OPTION_LETTERS = "ABCDEFGHIJ"


def _decision_ref(task: Task, basis: str) -> str:
    """Short, deterministic reference derived from the QUESTION's own
    content (not just the task) - two different questions on the same
    task get two different refs, while retrying the identical question
    reproduces the same ref (Review Task #80).

    Used for two things: (1) embedded in the human-facing message so a
    reply can cite which pending decision it answers when a task has
    more than one (message_id/reply association is #31's job - this is
    the simpler alternative Codex's review explicitly allowed); (2) as
    the suffix of the default correlation_id (see create_pending_decision)
    so a second, distinct question never overwrites/inherits the
    resolved=1 state of an earlier, already-answered one for the same
    task.
    """
    return hashlib.sha256(f"{task.id}:{basis}".encode("utf-8")).hexdigest()[:8]


def format_decision_message(
    task: Task,
    problem: str,
    why: str,
    options: list[str],
    recommendation: str,
    impact: str,
) -> str:
    """Formats a NEEDS_LUCAS message using the exact section-30 template:
    PROJETO / CONTEXTO / PROBLEMA / POR QUE PRECISA DE MIM / OPCOES (A/B/C)
    / RECOMENDACAO / IMPACTO.

    `why` ("por que precisa de mim") isn't in the issue's own example
    signature (`format_decision_message(task, problem, options,
    recommendation, impact)`) but IS a required section of the template
    with no other sensible source (task/problem alone don't explain why
    THIS specifically needs a human) - added as an explicit parameter
    rather than silently omitting a required section.
    """
    project_line = task.project_id or "desconhecido"
    context_line = task.context or task.objective

    lettered_options = "\n".join(
        f"{_OPTION_LETTERS[i]}) {option}" for i, option in enumerate(options)
    )

    # The ref must be sensitive to every field a human could tell two
    # decisions apart by, not just `problem` - two questions with the
    # same problem text but different options/recommendation/impact are
    # still different decisions and must not display the same REF, even
    # though their persisted correlation_id (hashed from the full
    # rendered message in create_pending_decision) already differed
    # (Review Task #80, 2nd revalidation).
    ref = _decision_ref(task, "\x1f".join([problem, why, *options, recommendation, impact]))

    return (
        f"REF: {ref}\n\n"
        f"PROJETO:\n{project_line}\n\n"
        f"CONTEXTO:\n{context_line}\n\n"
        f"PROBLEMA:\n{problem}\n\n"
        f"POR QUE PRECISA DE MIM:\n{why}\n\n"
        f"OPCOES:\n{lettered_options}\n\n"
        f"RECOMENDACAO:\n{recommendation}\n\n"
        f"IMPACTO:\n{impact}"
    )


def create_pending_decision(
    task: Task,
    message: str,
    store: Store,
    correlation_id: str | None = None,
) -> tuple[bool, str | None]:
    """Registers the decision in persistence (#13's `decisions` table, so
    it survives a restart) and sends it over the Telegram control
    channel (#19). Returns (ok, error) from the send - the decision is
    ALWAYS persisted regardless of whether Telegram delivery succeeds,
    so a Telegram outage never loses the pending decision itself (it can
    still be surfaced through voice, #27, once that exists).

    Defaults `correlation_id` to a hash of (task, message) rather than
    bare `task.correlation_id` - reusing the task's own fixed id for
    EVERY decision on that task meant a second, different question
    silently overwrote the first row via the decisions table's UPSERT,
    inheriting its resolved=1 state and vanishing from the pending queue
    even though nobody answered the new question (Review Task #80).
    Retrying the IDENTICAL message still maps to the same correlation_id
    (idempotent - no duplicate row), since the hash is deterministic."""
    correlation_id = correlation_id or f"{task.correlation_id}:{_decision_ref(task, message)}"
    store.save_decision(correlation_id=correlation_id, task_id=task.id, message=message)
    return send_control_message(message)


def notify_needs_lucas(
    task: Task,
    problem: str,
    why: str,
    options: list[str],
    recommendation: str,
    impact: str,
    store: Store,
) -> tuple[bool, str | None]:
    """Convenience entrypoint combining format + create in one call - the
    single function callers (planner #22, review pipeline #29, deploy/bug
    detection #38) invoke automatically when a task transitions to
    NEEDS_LUCAS. Wiring those callers to actually invoke this on every
    such transition is #23's job (out of scope here, per the issue's own
    OUT OF SCOPE note - this issue only formats and routes)."""
    message = format_decision_message(task, problem, why, options, recommendation, impact)
    return create_pending_decision(task, message, store)


def resolve_pending_decision(correlation_id: str, response: str, store: Store) -> dict | None:
    """Routes Lucas's response back to the task it was blocking. Returns
    the resolved decision's info (task_id included) so the caller (#31's
    intent parser, once it exists) can resume the right task/agent - or
    None if no pending decision matches `correlation_id` (already
    resolved, or never existed - never raises)."""
    pending = {d["correlation_id"]: d for d in store.get_pending_decisions()}
    decision = pending.get(correlation_id)
    if decision is None:
        return None
    store.resolve_decision(correlation_id, response)
    return decision


@dataclass
class ControlResult:
    kind: str
    message: str
    task_id: str | None = None
    correlation_id: str | None = None
    plan: PlanResult | None = None
    delivered: bool = False


def _normalized(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c)).strip()


def get_priority_queue(store: Store) -> list[Task]:
    """Eligible tasks in priority order; never bypass dependencies or blockers."""
    ranks = {'urgent': 0, 'high': 1, 'medium': 2, 'low': 3}
    return sorted(get_promotable_tasks(store.list_tasks()),
                  key=lambda task: (ranks.get(task.priority, 2), task.created_at, task.id))


def _decision_aliases(decision: dict) -> set[str]:
    aliases = {decision['correlation_id'].casefold()}
    if decision['task_id']:
        aliases.add(decision['task_id'].casefold())
    ref = re.match(r'REF: ([a-f0-9]{8})\n', decision['message'], re.IGNORECASE)
    if ref:
        aliases.add(ref[1].casefold())
    return aliases


def _answer_decision(store: Store, decision: dict, response: str) -> ControlResult:
    """Commit answer, safe readmission and event together; no network in TX."""
    def apply(connection):
        row = connection.execute(
            'SELECT task_id, message FROM decisions WHERE correlation_id=? AND resolved=0',
            (decision['correlation_id'],)).fetchone()
        if not row or row != (decision['task_id'], decision['message']):
            return ControlResult('clarification', 'Essa decisao mudou ou ja foi respondida. Confira a referencia.')
        task_row = connection.execute('SELECT data FROM tasks WHERE id=?', (row[0],)).fetchone()
        if not task_row:
            return ControlResult('clarification', 'A tarefa dessa decisao ainda nao esta na fila. A resposta nao foi consumida.')
        task = Task.from_dict(json.loads(task_row[0]))
        if task.state in (TaskState.DONE, TaskState.FAILED):
            return ControlResult('clarification', 'A tarefa ja foi encerrada. Confira a decisao antes de responder.')
        now = datetime.now(timezone.utc)
        connection.execute(
            'UPDATE decisions SET resolved=1, response=?, resolved_at=? WHERE correlation_id=?',
            (response, now.isoformat(), decision['correlation_id']))
        remaining = connection.execute(
            'SELECT 1 FROM decisions WHERE task_id=? AND resolved=0 LIMIT 1', (task.id,)).fetchone()
        resume = task.state == TaskState.NEEDS_LUCAS and not remaining
        # Preserve the human answer with the task as well as the event, so
        # the eventual executor receives it (including a refusal) on resumption.
        task.context += f'\n\nResposta de Lucas ({decision["correlation_id"]}):\n{response}'
        task.updated_at = now
        if resume:
            # Re-enter admission, not READY: dependencies, cutoff and agent
            # availability still belong to scheduler/queue/executor policy.
            task.state = TaskState.PLANNED
        connection.execute('UPDATE tasks SET state=?, data=? WHERE id=?',
                           (task.state.value, json.dumps(task.to_dict()), task.id))
        emit_in_transaction(connection, EventType.DECISION_RECEIVED,
                            {'task_id': task.id, 'response': response,
                             'preferred_agent': task.preferred_agent.value,
                             'resume_requested': resume},
                            correlation_id=decision['correlation_id'], project_id=task.project_id,
                            created_at=now)
        message = 'Resposta registrada.'
        if resume:
            message += ' A tarefa voltou ao planejamento para readmissao na fila.'
        elif remaining:
            message += ' Essa tarefa ainda tem outra decisao pendente.'
        return ControlResult('decision', message, task.id, decision['correlation_id'])
    return store.run_in_transaction(apply)


def _prioritize(store: Store, target: str) -> ControlResult:
    target = target.strip().strip('"')
    tasks = [task for task in store.list_tasks()
             if task.id.casefold() == target.casefold() or _normalized(task.title) == _normalized(target)]
    if len(tasks) != 1:
        return ControlResult('clarification', 'Indique o ID completo ou o titulo exato de uma unica tarefa para priorizar.')
    def apply(connection):
        row = connection.execute('SELECT data FROM tasks WHERE id=?', (tasks[0].id,)).fetchone()
        if not row:
            return ControlResult('clarification', 'A tarefa nao esta mais na fila.')
        task = Task.from_dict(json.loads(row[0]))
        if task.state in (TaskState.IN_PROGRESS, TaskState.IN_REVIEW, TaskState.DONE, TaskState.FAILED):
            return ControlResult('clarification', 'Essa tarefa ja foi iniciada ou encerrada; a fila nao foi alterada.')
        task.priority = 'urgent'
        task.updated_at = datetime.now(timezone.utc)
        connection.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task.to_dict()), task.id))
        return ControlResult('priority', 'Prioridade da tarefa atualizada para urgente; bloqueios e dependencias preservados.', task.id)
    return store.run_in_transaction(apply)


def _route_control(text: str, store: Store, plan_fn: Callable) -> ControlResult:
    normalized = _normalized(text)
    priority = re.match(r'^(?:prioriza|priorize|priorizar)\s+(?:a\s+)?(?:tarefa\s+)?(.+)$', text, re.IGNORECASE)
    if priority:
        return _prioritize(store, priority[1])
    pending = store.get_pending_decisions()
    # Explicit reply grammar accepts free text, but requires an exact unique
    # REF, decision correlation_id, or task_id. No fuzzy matching of identities.
    explicit = re.match(r'^(?:ref\s*:?|resposta|responder)\s+([^\s]+)\s+(.+)$', text, re.IGNORECASE | re.DOTALL)
    if explicit:
        alias, response = explicit[1].rstrip(':').casefold(), explicit[2].strip()
        matches = [d for d in pending if alias in _decision_aliases(d)]
    elif re.fullmatch(r'(?:[a-j]|sim|nao|aprovo|rejeito|(?:pode usar (?:a )?)?opcao\s+[a-j])[.!]?', normalized):
        matches, response = pending, text
    else:
        matches, response = None, None
    if matches is not None:
        if len(matches) != 1:
            return ControlResult('clarification', 'Indique uma decisao pendente com REF: <referencia> <resposta>. Nenhuma resposta foi aplicada.')
        # Validate option letters against the actual pending question. Plain
        # yes/no and explicitly cited prose remain human answers, not commands.
        option = re.fullmatch(r'(?:(?:pode usar (?:a )?)?opcao\s+)?([a-j])[.!]?', _normalized(response))
        if option and not re.search(rf'^{option[1].upper()}\) ', matches[0]['message'], re.MULTILINE):
            return ControlResult('clarification', 'Essa opcao nao aparece na decisao. Confira as opcoes antes de responder.')
        return _answer_decision(store, matches[0], response)
    is_objective = re.match(r'^(?:objetivo\s*:|quero que\s+|cria(?:r)?\s+|crie\s+|implementa(?:r)?\s+|implemente\s+|corrig[ae]\s+|corrigir\s+|adiciona(?:r)?\s+|adicione\s+)', normalized)
    if not is_objective:
        return ControlResult('clarification', 'Nao consegui distinguir objetivo, resposta ou prioridade. Use Objetivo: <pedido>, REF: <referencia> <resposta> ou Prioriza <ID da tarefa>.')
    objective = re.sub(r'^objetivo\s*:\s*', '', text, flags=re.IGNORECASE).strip()
    if not objective:
        return ControlResult('clarification', 'Descreva o objetivo que deseja planejar.')
    try:
        result = plan_fn(objective, store=store)
    except Exception:
        # LLM/provider errors can contain credentials or full request bodies.
        return ControlResult('error', 'Nao consegui gerar o plano agora. Tente novamente mais tarde.')
    if result.needs_human_decision:
        return ControlResult('plan', 'O planner identificou uma decisao humana necessaria; nenhuma tarefa foi iniciada.', plan=result)
    return ControlResult('plan', f'Planejamento gerou {len(result.tasks)} tarefa(s).', plan=result)


def handle_control_message(text: str, *, store: Store | None = None,
                           plan_fn: Callable | None = None,
                           send_fn: Callable | None = None) -> ControlResult:
    """Route text already authenticated by #19's control-chat filter.

    Return the plan to #23/runtime for persistence/dispatch. Decision replies
    persist atomically and emit DECISION_RECEIVED for that exact correlation;
    the last answer readmits only NEEDS_LUCAS tasks, preserving agent assignment.
    Ambiguous text asks for clarification instead of guessing an action.
    """
    owned = store is None
    active_store = store if store is not None else Store()
    try:
        try:
            result = (_route_control(text.strip(), active_store, plan_fn or plan)
                      if isinstance(text, str) and text.strip() else
                      ControlResult('clarification', 'Envie um objetivo, resposta ou comando de prioridade em texto.'))
        except sqlite3.Error:
            result = ControlResult('error', 'Nao consegui registrar a alteracao na fila. Tente novamente mais tarde.')
        try:
            result.delivered = bool((send_fn or send_control_message)(result.message)[0])
        except Exception:
            result.delivered = False
        return result
    finally:
        if owned:
            active_store.close()
