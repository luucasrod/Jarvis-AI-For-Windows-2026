"""Idle detection and diagnosis (issue #27).

Watches for the specific shape of "nobody is working": READY tasks are
waiting, at least one agent is free to take them, nothing is currently
IN_PROGRESS, and that stillness has lasted past a configurable grace
period (`OrchestratorConfig.idle_check_minutes`) - a brief gap between one
task finishing and the next starting is normal and must not alarm anyone.

Diagnosis runs in order of most-actionable first: is Paperclip even
reachable? Is the apparent stall actually just a READY task whose
dependencies were reopened after it was promoted (a data inconsistency,
not a bug)? Only when neither explains it does this escalate as a
genuinely unexplained stall via `EventType.DECISION_REQUIRED`, per the
issue's own scope - this module only investigates and reports, it never
restarts OS/Paperclip processes itself (out of scope, explicitly).

Shared with #36 (aggregated healthcheck), which is expected to build on
top of `check_idle()` rather than duplicate its logic.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import paperclip_client as client
from orchestrator.agent_availability import is_agent_available
from orchestrator.audit import record as audit_record
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit, query_events
from orchestrator.models import AgentName, TaskState
from orchestrator.persistence import Store
from orchestrator.task_queue import get_promotable_tasks

_ACTIVITY_EVENTS = [EventType.TASK_STARTED, EventType.TASK_COMPLETED]


@dataclass(frozen=True)
class IdleDiagnosis:
    cause: str
    detail: str
    ready_task_ids: tuple[str, ...]
    escalated: bool


def _now(clock: Callable[[], datetime] | None) -> datetime:
    instant = (clock or (lambda: datetime.now(timezone.utc)))()
    if instant.utcoffset() is None:
        raise ValueError("healthcheck clock must return a timezone-aware datetime")
    return instant.astimezone(timezone.utc)


def _last_activity_at(store: Store) -> datetime | None:
    events = query_events(store, event_types=_ACTIVITY_EVENTS)
    return events[-1]["created_at"] if events else None


def _record(store: Store, diagnosis: IdleDiagnosis) -> None:
    audit_record(
        store, action="idle_check", origin="healthcheck",
        result="escalated" if diagnosis.escalated else "diagnosed",
        extra={
            "cause": diagnosis.cause,
            "ready_task_ids": list(diagnosis.ready_task_ids),
        },
    )


def check_idle(
    store: Store,
    *,
    config: OrchestratorConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    paperclip_available: Callable[[], bool] | None = None,
) -> IdleDiagnosis | None:
    """Returns a diagnosis only when apparent idleness is real and either
    explained (Paperclip down, a dependency inconsistency) or genuinely
    unexplained - `None` whenever there simply is no stall to explain
    (no READY work, no free agent, work already in flight, or the quiet
    period hasn't crossed the threshold yet)."""
    cfg = config or load_config()
    now = _now(clock)

    all_tasks = store.list_tasks()
    ready = [task for task in all_tasks if task.state == TaskState.READY]
    if not ready:
        return None

    if any(task.state == TaskState.IN_PROGRESS for task in all_tasks):
        return None

    agent_free = (
        is_agent_available(store, AgentName.CLAUDE, clock=clock)
        or is_agent_available(store, AgentName.CODEX, clock=clock)
    )
    if not agent_free:
        return None

    last_activity = _last_activity_at(store)
    if last_activity is not None:
        elapsed_minutes = (now - last_activity).total_seconds() / 60
        if elapsed_minutes < cfg.idle_check_minutes:
            return None

    ready_ids = tuple(task.id for task in ready)

    check_paperclip = paperclip_available or client.is_available
    if not check_paperclip():
        diagnosis = IdleDiagnosis(
            cause="paperclip_unavailable",
            detail="Paperclip nao respondeu ao healthcheck - tarefas READY nao podem ser despachadas.",
            ready_task_ids=ready_ids,
            escalated=False,
        )
        _record(store, diagnosis)
        return diagnosis

    promotable_ids = {task.id for task in get_promotable_tasks(all_tasks)}
    blocked_ready_ids = tuple(task_id for task_id in ready_ids if task_id not in promotable_ids)
    if blocked_ready_ids:
        diagnosis = IdleDiagnosis(
            cause="dependency_blocked",
            detail="Tarefa(s) READY tem dependencia ainda nao concluida - inconsistencia de estado, nao ociosidade real.",
            ready_task_ids=blocked_ready_ids,
            escalated=False,
        )
        _record(store, diagnosis)
        return diagnosis

    diagnosis = IdleDiagnosis(
        cause="unexplained",
        detail="Tarefas READY, agente disponivel e Paperclip ok, mas nada em andamento ha mais tempo que o esperado.",
        ready_task_ids=ready_ids,
        escalated=True,
    )
    emit(
        store, EventType.DECISION_REQUIRED,
        {"kind": "idle_stall", "cause": diagnosis.cause, "ready_task_ids": list(ready_ids)},
    )
    _record(store, diagnosis)
    return diagnosis
