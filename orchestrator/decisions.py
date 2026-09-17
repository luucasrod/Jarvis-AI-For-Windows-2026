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
import uuid
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

_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_deliveries (
    correlation_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0
);
"""

# The `idempotency_keys` kind this module wrote BEFORE `decision_deliveries`
# existed (PR #132's original, since-replaced design) - a row here is a
# real confirmed send from that era and must be migrated, not ignored.
_LEGACY_DELIVERY_KIND = "decision_telegram_send"

# Reasons from telegram_bot._send_message that mean the message was
# PROVABLY never accepted (bad config, bad token, bad chat/content) -
# these are safe to retry for real. Everything else (offline, timeout,
# a 5xx, a malformed/unconfirmed response) is left UNCERTAIN: Telegram
# gives no way to ask "was this specific message already delivered?", so
# per Review Task #133 round 2 an uncertain outcome is never silently
# retried - the caller gets a distinct, explicit result instead.
_DEFINITELY_NOT_SENT_PREFIXES = (
    "Telegram nao configurado", "token invalido", "chat_id invalido",
)


def _send_decision_once(store: Store, correlation_id: str, message: str) -> tuple[bool, str | None]:
    """Sends `message` at most once for this `correlation_id`, closing the
    crash window Review Task #133 (round 2) found in the previous
    "record success after sending" design:

    1. A claim row is reserved BEFORE any network call, inside the same
       transaction primitive (`run_in_transaction`, BEGIN IMMEDIATE) used
       elsewhere in this codebase as a real cross-connection lock (#26/#28/
       #23's own fallback queue). Two concurrent callers for the same
       correlation_id can no longer both pass a check-then-send race
       (round-2 finding #3): only the one whose owner wins the INSERT OR
       IGNORE proceeds to send at all.
    2. A caller that finds an EXISTING, unconfirmed claim owned by someone
       else (a concurrent call, OR an earlier attempt that crashed/reopened
       the Store before confirming) never resends - the outcome of that
       earlier attempt is unknown and Telegram has no reconciliation query
       (round-2 finding #1: the previous design only recorded the key
       AFTER a confirmed send, leaving a real window where a crash between
       "Telegram accepted it" and "we recorded that" caused a genuine
       retry to resend). This trades "might occasionally leave a decision
       stuck as uncertain" for "never knowingly duplicates" - the
       documented, conservative choice Review Task #133 asked for, matching
       how an uncertain Paperclip/GitHub creation is surfaced rather than
       blindly retried.
    3. Only a reason PROVABLY unrelated to whether Telegram received the
       message (config missing, bad token, bad chat) releases the claim so
       a real retry can happen - a timeout or malformed response (round-2
       finding #2) leaves the claim in place, uncertain, forever (until a
       caller passes a fresh correlation_id on purpose).

    4. A confirmation from the PREVIOUS design (the plain `idempotency_keys`
       row this same function used to write, kind `decision_telegram_send`,
       before this claim-based table existed) is migrated into
       `decision_deliveries` as already-confirmed INSIDE this same claim
       transaction (round-3 finding: upgrading straight from that design
       ignored its already-proven-sent decisions entirely and resent them)
       - a message proven delivered under the old scheme is never resent
       just because the storage format changed underneath it.
    """
    store.ensure_schema(_DELIVERY_SCHEMA)
    owner = str(uuid.uuid4())

    def claim(connection: sqlite3.Connection):
        legacy_confirmed = connection.execute(
            "SELECT 1 FROM idempotency_keys WHERE correlation_id = ? AND kind = ?",
            (correlation_id, _LEGACY_DELIVERY_KIND),
        ).fetchone()
        if legacy_confirmed:
            connection.execute(
                "INSERT INTO decision_deliveries (correlation_id, owner, confirmed) VALUES (?, ?, 1) "
                "ON CONFLICT(correlation_id) DO UPDATE SET confirmed = 1",
                (correlation_id, owner),
            )
            return (owner, 1)
        connection.execute(
            "INSERT OR IGNORE INTO decision_deliveries (correlation_id, owner, confirmed) VALUES (?, ?, 0)",
            (correlation_id, owner),
        )
        return connection.execute(
            "SELECT owner, confirmed FROM decision_deliveries WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()

    claimed_owner, confirmed = store.run_in_transaction(claim)
    if confirmed:
        return True, None
    if claimed_owner != owner:
        return False, (
            "entrega incerta - uma tentativa anterior ou concorrente desta "
            "mesma decisao ainda nao foi confirmada; verifique manualmente "
            "antes de tentar de novo"
        )

    ok, error = send_control_message(message, store=store)
    if ok:
        def confirm(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE decision_deliveries SET confirmed = 1 WHERE correlation_id = ? AND owner = ?",
                (correlation_id, owner),
            )
        store.run_in_transaction(confirm)
        return True, None

    if error is not None and error.startswith(_DEFINITELY_NOT_SENT_PREFIXES):
        def release(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM decision_deliveries WHERE correlation_id = ? AND owner = ? AND confirmed = 0",
                (correlation_id, owner),
            )
        store.run_in_transaction(release)
    return ok, error


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
    return _send_decision_once(store, correlation_id, message)


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


_PENDING_PLAN_KEY = "orchestrator:pending_plan_confirmation"
_PLAN_CONFIRM_WORDS = re.compile(r'^(?:sim|confirmo|confirma|pode|manda|faz isso|isso mesmo)[.!]?$')
_PLAN_CANCEL_WORDS = re.compile(r'^(?:nao|cancela|cancelar|esquece|deixa quieto)[.!]?$')

# Issue #154: once a plan reply is resolved (confirmed, cancelled, or
# discarded for lack of confirm_fn) and the pending-plan pointer is
# cleared, an immediate DUPLICATE of the SAME reply text (a Telegram
# resend, a flaky retry, two poll-batch messages sent seconds apart by
# the same human) falls straight through the now-empty pending-plan
# branch into the bare sim/nao decision-answer grammar below - and can
# silently approve/reject an unrelated NEEDS_LUCAS decision that
# happened to be the only one pending, which was never the user's
# intent. This records the last resolved reply's exact normalized text
# and timestamp so an identical echo within a short window is caught
# and named as a duplicate instead of being routed onward.
_LAST_PLAN_REPLY_ECHO_KEY = "orchestrator:last_plan_reply_echo"
_PLAN_REPLY_ECHO_WINDOW_SECONDS = 15


def _record_plan_reply_echo(store: Store, normalized_text: str, *, now: datetime | None = None) -> None:
    at = (now or datetime.now(timezone.utc)).isoformat()
    store.set_sync_value(_LAST_PLAN_REPLY_ECHO_KEY, json.dumps({"text": normalized_text, "at": at}))


def _is_duplicate_plan_reply_echo(store: Store, normalized_text: str, *, now: datetime | None = None) -> bool:
    raw = store.get_sync_value(_LAST_PLAN_REPLY_ECHO_KEY)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("text") != normalized_text:
        return False
    try:
        resolved_at = datetime.fromisoformat(data.get("at", ""))
    except (TypeError, ValueError):
        return False
    elapsed = ((now or datetime.now(timezone.utc)) - resolved_at).total_seconds()
    return 0 <= elapsed <= _PLAN_REPLY_ECHO_WINDOW_SECONDS


def save_pending_plan(store: Store, *, objective: str, project_id: str | None, task_ids: list[str]) -> None:
    """Persists the ONE outstanding plan awaiting the user's confirmation
    (issue #152). Only reachable while NO plan is already pending -
    `_route_control`'s pending-plan branch intercepts every message
    (other than "sim"/"nao") while one is outstanding, so the user must
    explicitly confirm or cancel the current proposal before a new
    `Objetivo:` can reach this function. This is deliberate: replacing a
    pending plan with a new one mid-flight would leave its
    already-persisted PLANNED tasks silently orphaned with no path back
    to materializing or explicitly discarding them."""
    store.set_sync_value(_PENDING_PLAN_KEY, json.dumps({
        "objective": objective, "project_id": project_id, "task_ids": task_ids,
    }))


def load_pending_plan(store: Store) -> dict | None:
    raw = store.get_sync_value(_PENDING_PLAN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _clear_pending_plan(store: Store) -> None:
    store.set_sync_value(_PENDING_PLAN_KEY, "")


def _discard_pending_plan_tasks(store: Store, pending_plan: dict) -> None:
    """Deletes the PLANNED tasks a rejected/unexecutable pending plan
    already persisted (`save_pending_plan` writes them to the tasks table
    BEFORE confirmation, so `_route_control` can show their titles while
    asking "sim"/"nao"). Without this, `_clear_pending_plan` only removes
    the confirmation pointer - the tasks themselves stay PLANNED forever,
    with nothing else in the codebase to prune them. Since
    `execute_confirmed_plan` dispatches via `run_daily_cycle`, which
    admits/dispatches every PLANNED/READY task for the whole project (not
    just the just-confirmed task_ids), those orphans get silently swept
    into the NEXT confirmed plan for the same project and dispatched for
    real - creating a GitHub Issue and Paperclip assignment for a plan
    the user explicitly said "nao" to (found in review)."""
    for task_id in pending_plan.get("task_ids") or []:
        store.delete_task(task_id)


def _route_control(
    text: str, store: Store, plan_fn: Callable,
    fallback_fn: Callable | None = None, confirm_fn: Callable | None = None,
) -> ControlResult:
    normalized = _normalized(text)

    # Issue #152: a pending plan (from a PRIOR "Objetivo:" that already
    # got proposed but not yet confirmed) takes priority over everything
    # else in this function - a bare "sim"/"nao" answers THAT, not a
    # coincidentally-pending NEEDS_LUCAS decision, since it's the most
    # recent thing this conversation was asked to confirm.
    pending_plan = load_pending_plan(store)
    if pending_plan is not None:
        if _PLAN_CONFIRM_WORDS.match(normalized):
            if confirm_fn is None:
                _discard_pending_plan_tasks(store, pending_plan)
                _clear_pending_plan(store)
                _record_plan_reply_echo(store, normalized)
                return ControlResult('error', 'Nao ha um executor de planos configurado agora. O plano ficou sem efeito.')
            try:
                message = confirm_fn(pending_plan)
            except Exception:
                # Real GitHub/Paperclip errors can contain tokens/URLs.
                # The pending plan is deliberately KEPT (not cleared) on
                # failure - execute_confirmed_plan reuses the SAME
                # already-persisted task_ids/correlation_ids on every
                # call, so materialize_plan's own correlation_id-based
                # idempotency (#17) makes a second "sim" a genuine safe
                # retry, never a duplicate Issue, even if the first
                # attempt partially succeeded (e.g. GitHub created but
                # Paperclip dispatch then failed).
                return ControlResult('error', 'Nao consegui executar o plano agora. Nenhuma tarefa foi perdida - responda "sim" de novo para tentar outra vez.')
            _clear_pending_plan(store)
            _record_plan_reply_echo(store, normalized)
            return ControlResult('plan_executed', message or 'Plano executado.')
        if _PLAN_CANCEL_WORDS.match(normalized):
            _discard_pending_plan_tasks(store, pending_plan)
            _clear_pending_plan(store)
            _record_plan_reply_echo(store, normalized)
            return ControlResult('plan_cancelled', 'Cancelado, senhor. O plano nao foi executado.')
        return ControlResult(
            'clarification',
            'Ha um plano aguardando confirmacao. Responda "sim" para executar ou "nao" para cancelar.',
        )

    # Issue #154: an identical "sim"/"nao" that arrives right after ONE OF
    # THOSE SAME WORDS just resolved a plan confirmation/cancellation is
    # almost certainly a duplicate echo (a Telegram resend, both messages
    # of a flaky retry landing in the same poll batch) - not a fresh,
    # separately-intended reply to whatever NEEDS_LUCAS decision happens
    # to be pending now. Routing it onward could silently approve/reject
    # a decision the user never meant to touch (found in review).
    if _is_duplicate_plan_reply_echo(store, normalized):
        return ControlResult(
            'duplicate_ignored',
            'Ja processei essa confirmacao, senhor - nada foi feito de novo. '
            'Se quiser responder a outra coisa pendente, use REF: <referencia> <resposta>.',
        )

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
    # "objetivo" tolerates a missing colon (issue #148): speech-to-text
    # transcription of a spoken command naturally drops punctuation, and
    # every OTHER trigger verb here ("quero que", "cria", "implementa", ...)
    # already needs no colon at all - "objetivo" alone required one, which
    # silently rejected a genuine spoken "Objetivo testar..." command.
    is_objective = re.match(r'^(?:objetivo\s*:\s*|objetivo\s+|quero que\s+|cria(?:r)?\s+|crie\s+|implementa(?:r)?\s+|implemente\s+|corrig[ae]\s+|corrigir\s+|adiciona(?:r)?\s+|adicione\s+)', normalized)
    if not is_objective:
        # Issue #149: text that matches NONE of the structured shapes above
        # (no side effect has happened yet on this path - every branch that
        # DOES mutate state returns before reaching here) falls to free
        # conversation instead of a bare "nao entendi", when the caller
        # wired one in. Never invents state - see conversation.py.
        if fallback_fn is not None:
            answer = fallback_fn(text)
            if answer:
                return ControlResult('conversation', answer)
        return ControlResult('clarification', 'Nao consegui distinguir objetivo, resposta ou prioridade. Use Objetivo: <pedido>, REF: <referencia> <resposta> ou Prioriza <ID da tarefa>.')
    objective = re.sub(r'^objetivo\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
    if not objective:
        return ControlResult('clarification', 'Descreva o objetivo que deseja planejar.')
    try:
        result = plan_fn(objective, store=store)
    except Exception:
        # LLM/provider errors can contain credentials or full request bodies.
        return ControlResult('error', 'Nao consegui gerar o plano agora. Tente novamente mais tarde.')
    if result.needs_human_decision:
        return ControlResult('plan', 'O planner identificou uma decisao humana necessaria; nenhuma tarefa foi iniciada.', plan=result)
    if not result.tasks:
        return ControlResult('plan', 'O planejamento nao gerou nenhuma tarefa.', plan=result)
    # Issue #152: planning alone never touches GitHub/Paperclip - tasks
    # are persisted (so they exist and can be inspected/cancelled even if
    # the user never confirms) but nothing is materialized or dispatched
    # until an explicit "sim" answers THIS proposal. Real Issues and real
    # agent assignments are exactly the kind of hard-to-reverse, external
    # action that needs the user's own confirmation, not an inferred one.
    for task in result.tasks:
        store.save_task(task)
    save_pending_plan(
        store, objective=objective, project_id=result.project_id,
        task_ids=[task.id for task in result.tasks],
    )
    titles = "\n".join(f"- {task.title}" for task in result.tasks[:10])
    extra = f"\n(e mais {len(result.tasks) - 10})" if len(result.tasks) > 10 else ""
    return ControlResult(
        'plan_proposed',
        f"Plano para \"{objective}\" - {len(result.tasks)} tarefa(s):\n{titles}{extra}\n\n"
        "Confirma, senhor? Responda \"sim\" para mandar pro GitHub e despachar pros agentes, "
        "ou \"nao\" para cancelar.",
        plan=result,
    )


def handle_control_message(text: str, *, store: Store | None = None,
                           plan_fn: Callable | None = None,
                           send_fn: Callable | None = None,
                           fallback_fn: Callable | None = None,
                           confirm_fn: Callable | None = None) -> ControlResult:
    """Route text already authenticated by #19's control-chat filter.

    Return the plan to #23/runtime for persistence/dispatch. Decision replies
    persist atomically and emit DECISION_RECEIVED for that exact correlation;
    the last answer readmits only NEEDS_LUCAS tasks, preserving agent assignment.
    Ambiguous text asks for clarification instead of guessing an action,
    unless `fallback_fn` (issue #149's free-conversation answer) is given -
    then it answers instead of just asking the sender to rephrase.

    A successful "Objetivo:" plan is never materialized/dispatched here
    directly - it is proposed and PERSISTED as pending, and only a later
    "sim" (routed to `confirm_fn`, issue #152) actually creates real
    GitHub Issues and assigns real Paperclip agents.
    """
    owned = store is None
    active_store = store if store is not None else Store()
    try:
        try:
            result = (_route_control(text.strip(), active_store, plan_fn or plan, fallback_fn, confirm_fn)
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
