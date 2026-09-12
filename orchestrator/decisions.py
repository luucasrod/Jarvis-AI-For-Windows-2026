"""Human-decision (NEEDS_LUCAS) messaging and routing (issue #33).

Formats decision messages using the exact template from section 30, and
registers+sends them so Lucas can respond over the Telegram control
channel (#19). Also provides the resolution half: routing Lucas's
response back to the pending decision (and, transitively, the task it
blocks) so other independent tasks keep progressing while this one waits
(reinforces #21's/#20's continuity guarantee - a blocked task never
blocks unrelated ones).

`orchestrator/decisions.py` is also expected to host
`handle_control_message()` - the Telegram control-channel INTENT PARSER
for brand-new objectives Lucas sends (distinct from a response to an
already-pending decision) - added by issue #31, not implemented here
(different scope; this module only knows about NEEDS_LUCAS messaging).
"""
from __future__ import annotations

import hashlib

from orchestrator.models import Task
from orchestrator.persistence import Store
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
