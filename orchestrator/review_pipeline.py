"""Cross-review policy + 3-failure escalation (issue #29).

Cross-review (section 24): whoever implemented a task never approves it
alone - Codex implements -> Claude reviews, and vice versa (assigned by
#24's classify_task). This module does NOT perform the review itself (a
human or another agent session judges pass/fail); it processes that
verdict and decides the next transition:

  FAIL #1 -> same implementer retries with the review feedback
  FAIL #2 -> switch to fallback_agent (a different approach/agent)
  FAIL #3 -> escalate: emit a "CEO escalation needed" signal and register
             a NEEDS_LUCAS decision, before anyone asks the human directly

  PASS     -> clear the failure counter, task is ready for the next step
              (merge, issue #30)

Researching documentation/the web before escalating (section 26) is the
executing agent's own behavior during implementation, not something this
pipeline does - explicitly out of scope per the issue.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orchestrator.events import EventType, emit
from orchestrator.models import AgentName, Task, TaskState
from orchestrator.persistence import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_failures (
    task_id TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0
);
"""


class ReviewOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True)
class ReviewResult:
    outcome: ReviewOutcome
    failure_count: int
    escalate_to_ceo: bool
    needs_lucas: bool
    next_implementer: AgentName | None
    recommended_state: TaskState


def _ensure_table(store: Store) -> None:
    store.ensure_schema(_SCHEMA)


def _get_failure_count(store: Store, task_id: str) -> int:
    _ensure_table(store)
    rows = store.query("SELECT failure_count FROM review_failures WHERE task_id = ?", (task_id,))
    return rows[0][0] if rows else 0


def _set_failure_count(store: Store, task_id: str, count: int) -> None:
    _ensure_table(store)
    store.execute(
        "INSERT INTO review_failures (task_id, failure_count) VALUES (?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET failure_count=excluded.failure_count",
        (task_id, count),
    )


def _clear_failure_count(store: Store, task_id: str) -> None:
    _ensure_table(store)
    store.execute("DELETE FROM review_failures WHERE task_id = ?", (task_id,))


def _opposite_agent(agent: AgentName) -> AgentName:
    if agent == AgentName.CLAUDE:
        return AgentName.CODEX
    if agent == AgentName.CODEX:
        return AgentName.CLAUDE
    return AgentName.EITHER


def request_review(
    task: Task,
    implementer: AgentName,
    reviewer: AgentName,
    passed: bool,
    store: Store,
    feedback: str = "",
) -> ReviewResult:
    """Processes the verdict of an already-performed independent review
    (reviewer must differ from implementer - enforced by #24's
    classify_task upstream, not re-checked here) and decides the next
    transition per the 3-failure escalation policy.

    `passed` is supplied by the caller (a human, or another agent
    session's judgement) - this function's own job is purely the state
    machine around that verdict, not performing the review."""
    emit(
        store,
        EventType.REVIEW_REQUESTED,
        {"task_id": task.id, "implementer": implementer.value, "reviewer": reviewer.value},
        correlation_id=task.correlation_id,
        project_id=task.project_id,
    )

    if passed:
        emit(
            store,
            EventType.REVIEW_PASSED,
            {"task_id": task.id, "reviewer": reviewer.value},
            correlation_id=task.correlation_id,
            project_id=task.project_id,
        )
        _clear_failure_count(store, task.id)
        return ReviewResult(
            outcome=ReviewOutcome.PASS,
            failure_count=0,
            escalate_to_ceo=False,
            needs_lucas=False,
            next_implementer=None,
            recommended_state=TaskState.DONE,
        )

    failure_count = _get_failure_count(store, task.id) + 1
    _set_failure_count(store, task.id, failure_count)
    emit(
        store,
        EventType.REVIEW_FAILED,
        {"task_id": task.id, "failure_count": failure_count, "feedback": feedback},
        correlation_id=task.correlation_id,
        project_id=task.project_id,
    )

    if failure_count == 1:
        return ReviewResult(
            outcome=ReviewOutcome.FAIL,
            failure_count=1,
            escalate_to_ceo=False,
            needs_lucas=False,
            next_implementer=implementer,
            recommended_state=TaskState.IN_PROGRESS,
        )

    if failure_count == 2:
        next_agent = task.fallback_agent if task.fallback_agent != AgentName.NONE else _opposite_agent(implementer)
        return ReviewResult(
            outcome=ReviewOutcome.FAIL,
            failure_count=2,
            escalate_to_ceo=False,
            needs_lucas=False,
            next_implementer=next_agent,
            recommended_state=TaskState.IN_PROGRESS,
        )

    # 3rd failure (or more, if somehow re-entered without resetting): escalate.
    store.save_decision(
        correlation_id=task.correlation_id,
        task_id=task.id,
        message=(
            f"Tarefa '{task.title}' falhou revisao {failure_count}x. "
            "Precisa de escalonamento tecnico (diagnostico/pesquisa/decomposicao "
            "pelo CEO) antes de qualquer intervencao de Lucas."
        ),
    )
    emit(
        store,
        EventType.DECISION_REQUIRED,
        {"task_id": task.id, "reason": f"{failure_count} falhas de revisao consecutivas"},
        correlation_id=task.correlation_id,
        project_id=task.project_id,
    )
    return ReviewResult(
        outcome=ReviewOutcome.FAIL,
        failure_count=failure_count,
        escalate_to_ceo=True,
        needs_lucas=True,
        next_implementer=None,
        recommended_state=TaskState.NEEDS_LUCAS,
    )
