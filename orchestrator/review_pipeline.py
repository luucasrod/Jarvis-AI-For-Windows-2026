"""Cross-review policy + 3-failure escalation (issue #29).

Cross-review (section 24): whoever implemented a task never approves it
alone - Codex implements -> Claude reviews, and vice versa (assigned by
#24's classify_task). This module does NOT perform the review itself (a
human or another agent session judges pass/fail); it processes that
verdict and decides the next transition:

  FAIL #1 -> same implementer retries with the review feedback
  FAIL #2 -> switch to an agent genuinely different from whoever actually
             implemented (task.fallback_agent, unless it's the same agent
             that just failed - then the true opposite is used instead)
  FAIL #3 -> emit a "CEO escalation needed" signal (state BLOCKED) - a
             human decision is NOT created yet. Only escalate_to_human(),
             called once the CEO's diagnosis concludes a human is
             actually needed, registers the NEEDS_LUCAS decision.

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


def _increment_failure_count(store: Store, task_id: str) -> int:
    """Atomically increments and returns the new count in one statement -
    a separate get-then-set (even with a thread-safe Store) has a lost-
    update race between the two calls (#29, Review Task #66)."""
    _ensure_table(store)
    rows = store.execute_returning(
        "INSERT INTO review_failures (task_id, failure_count) VALUES (?, 1) "
        "ON CONFLICT(task_id) DO UPDATE SET failure_count = failure_count + 1 "
        "RETURNING failure_count",
        (task_id,),
    )
    return rows[0][0]


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
    *,
    head_sha: str | None = None,
) -> ReviewResult:
    """Processes the verdict of an already-performed independent review
    (reviewer must differ from implementer - #24's classify_task only
    RECOMMENDS this upstream; this function validates the concrete
    implementer/reviewer it actually receives, see below) and decides
    the next transition per the 3-failure escalation policy.

    `passed` is supplied by the caller (a human, or another agent
    session's judgement) - this function's own job is purely the state
    machine around that verdict, not performing the review.

    `head_sha`, when the caller can supply it (a task backed by a PR),
    is recorded on the REVIEW_PASSED event so a consumer - #37's
    merge_policy, specifically - can verify the review it's relying on
    was actually performed against the EXACT commit it's about to merge,
    not just "this task, at some point, passed review" (added per Review
    Task #111's demand for a durable, checkable attestation rather than
    a caller-supplied claim alone).

    Raises ValueError if implementer/reviewer aren't two distinct,
    concrete agents - classify_task only RECOMMENDS who should review,
    it never validates who actually shows up here, so a caller passing
    the same agent twice (or EITHER/NONE, which isn't a real reviewer)
    must not be able to make a task self-approve (#29, Review Task #66).
    """
    if implementer in (AgentName.EITHER, AgentName.NONE):
        raise ValueError(f"implementer must be a concrete agent, got {implementer}")
    if reviewer in (AgentName.EITHER, AgentName.NONE):
        raise ValueError(f"reviewer must be a concrete agent, got {reviewer}")
    if implementer == reviewer:
        raise ValueError("reviewer must differ from implementer (no self-review)")

    emit(
        store,
        EventType.REVIEW_REQUESTED,
        {"task_id": task.id, "implementer": implementer.value, "reviewer": reviewer.value},
        correlation_id=task.correlation_id,
        project_id=task.project_id,
    )

    if passed:
        payload = {"task_id": task.id, "reviewer": reviewer.value}
        if head_sha is not None:
            payload["head_sha"] = head_sha
        emit(
            store,
            EventType.REVIEW_PASSED,
            payload,
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

    failure_count = _increment_failure_count(store, task.id)
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
        # fallback_agent is fixed at task-creation time and may already
        # equal the CURRENT implementer (e.g. preferred=Claude,
        # fallback=Codex, but Codex already implemented as the fallback
        # for failure #1) - the rule is "switch agent/approach", so a
        # fallback that matches who's actually implementing right now is
        # not a real switch and must fall through to the true opposite
        # (#29, Review Task #66). Only CLAUDE/CODEX count as a concrete
        # fallback - EITHER isn't an agent that can actually pick this up
        # (request_review itself rejects it as implementer/reviewer), so
        # it must fall through to the opposite too, same as NONE
        # (Review Task #66, 2nd revalidation).
        next_agent = (
            task.fallback_agent
            if task.fallback_agent in (AgentName.CLAUDE, AgentName.CODEX) and task.fallback_agent != implementer
            else _opposite_agent(implementer)
        )
        return ReviewResult(
            outcome=ReviewOutcome.FAIL,
            failure_count=2,
            escalate_to_ceo=False,
            needs_lucas=False,
            next_implementer=next_agent,
            recommended_state=TaskState.IN_PROGRESS,
        )

    # 3rd failure (or more, if somehow re-entered without resetting):
    # signal that CEO diagnosis is needed FIRST - a human decision
    # (NEEDS_LUCAS/save_decision) must wait for that diagnosis to
    # conclude it's actually needed, per section 26/#29 scope. Getting
    # this wrong made every 3rd failure page a human immediately, with
    # no room for the CEO to research/decompose/retry first (#29, Review
    # Task #66). escalate_to_human() below is the explicit follow-up
    # transition once the CEO's diagnosis concludes a human is required.
    emit(
        store,
        EventType.CEO_ESCALATION_REQUIRED,
        {"task_id": task.id, "reason": f"{failure_count} falhas de revisao consecutivas"},
        correlation_id=task.correlation_id,
        project_id=task.project_id,
    )
    return ReviewResult(
        outcome=ReviewOutcome.FAIL,
        failure_count=failure_count,
        escalate_to_ceo=True,
        needs_lucas=False,
        next_implementer=None,
        recommended_state=TaskState.BLOCKED,
    )


def escalate_to_human(task: Task, store: Store, reason: str) -> ReviewResult:
    """Explicit follow-up transition for after the CEO's diagnosis (see
    request_review's 3rd-failure branch) concludes the task genuinely
    needs Lucas - only THIS creates the human-facing decision/event,
    never the 3rd review failure by itself (#29, Review Task #66)."""
    failure_count = _get_failure_count(store, task.id)
    store.save_decision(
        correlation_id=task.correlation_id,
        task_id=task.id,
        message=(
            f"Tarefa '{task.title}' falhou revisao {failure_count}x e o "
            f"diagnostico do CEO concluiu que precisa de Lucas: {reason}"
        ),
    )
    emit(
        store,
        EventType.DECISION_REQUIRED,
        {"task_id": task.id, "reason": reason},
        correlation_id=task.correlation_id,
        project_id=task.project_id,
    )
    return ReviewResult(
        outcome=ReviewOutcome.FAIL,
        failure_count=failure_count,
        escalate_to_ceo=False,
        needs_lucas=True,
        next_implementer=None,
        recommended_state=TaskState.NEEDS_LUCAS,
    )
