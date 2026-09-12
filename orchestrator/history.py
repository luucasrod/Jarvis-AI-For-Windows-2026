"""History module: 'o que mudou desde ontem' (issue #34).

Built entirely on top of #14's event log (orchestrator.events) - this
module does no event collection of its own, only aggregation and a short
voice-friendly summary. Used by the voice facade (#27) and potentially by
the daily Telegram report (#32).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from orchestrator.events import EventType, query_events
from orchestrator.persistence import Store

_COUNTED_EVENT_TYPES = (
    EventType.TASK_CREATED,
    EventType.TASK_COMPLETED,
    EventType.REVIEW_PASSED,
    EventType.REVIEW_FAILED,
    EventType.BUG_FOUND,
    EventType.DEPLOYMENT_FINISHED,
    EventType.DECISION_REQUIRED,
    EventType.DECISION_RECEIVED,
    EventType.MERGE_COMPLETED,
)


@dataclass
class HistorySummary:
    since: datetime
    tasks_created: int = 0
    tasks_completed: int = 0
    decisions_pending: int = 0
    reviews_passed: int = 0
    reviews_failed: int = 0
    bugs_found: int = 0
    deployments_finished: int = 0
    merges_completed: int = 0
    by_project: dict[str, dict[str, int]] = field(default_factory=dict)
    total_events: int = 0


def diff_since(store: Store, since: datetime) -> HistorySummary:
    """`decisions_pending` counts DECISION_REQUIRED events in the window
    whose correlation_id has NOT since seen a matching DECISION_RECEIVED
    (also in the window) - a decision that was asked AND answered within
    the same period must not be reported as currently awaiting a
    response (Review Task #68). This is deliberately about decisions
    specifically, not a general notion of "blocked": #14's event log has
    no dedicated BLOCKED event, and a task can be genuinely stuck
    (dependency/cycle) without ever generating a DECISION_REQUIRED - this
    module has no signal for that case and does not claim to.
    """
    events = query_events(store, since=since, event_types=list(_COUNTED_EVENT_TYPES))
    summary = HistorySummary(since=since, total_events=len(events))
    pending_decision_correlations: set[str] = set()

    for event in events:
        event_type = event["event_type"]
        project_id = event["project_id"] or "sem_projeto"
        correlation_id = event.get("correlation_id")

        project_counts = summary.by_project.setdefault(project_id, {})
        project_counts[event_type.value] = project_counts.get(event_type.value, 0) + 1

        if event_type == EventType.TASK_CREATED:
            summary.tasks_created += 1
        elif event_type == EventType.TASK_COMPLETED:
            summary.tasks_completed += 1
        elif event_type == EventType.REVIEW_PASSED:
            summary.reviews_passed += 1
        elif event_type == EventType.REVIEW_FAILED:
            summary.reviews_failed += 1
        elif event_type == EventType.BUG_FOUND:
            summary.bugs_found += 1
        elif event_type == EventType.DEPLOYMENT_FINISHED:
            summary.deployments_finished += 1
        elif event_type == EventType.MERGE_COMPLETED:
            summary.merges_completed += 1
        elif event_type == EventType.DECISION_REQUIRED:
            if correlation_id:
                pending_decision_correlations.add(correlation_id)
        elif event_type == EventType.DECISION_RECEIVED:
            if correlation_id:
                pending_decision_correlations.discard(correlation_id)

    summary.decisions_pending = len(pending_decision_correlations)
    return summary


def summarize_for_voice(summary: HistorySummary) -> str:
    if summary.total_events == 0:
        return "Nada relevante aconteceu nesse periodo."

    parts = []
    if summary.tasks_created:
        parts.append(f"{summary.tasks_created} tarefa(s) criada(s)")
    if summary.tasks_completed:
        parts.append(f"{summary.tasks_completed} concluida(s)")
    if summary.decisions_pending:
        parts.append(f"{summary.decisions_pending} decisao(oes) pendente(s)")
    if summary.reviews_passed:
        parts.append(f"{summary.reviews_passed} revisao(oes) aprovada(s)")
    if summary.reviews_failed:
        parts.append(f"{summary.reviews_failed} revisao(oes) reprovada(s)")
    if summary.bugs_found:
        parts.append(f"{summary.bugs_found} bug(s) encontrado(s)")
    if summary.deployments_finished:
        parts.append(f"{summary.deployments_finished} deploy(s) concluido(s)")
    if summary.merges_completed:
        parts.append(f"{summary.merges_completed} merge(s) concluido(s)")

    if not parts:
        return "Alguma atividade registrada, mas nada nas categorias principais."

    return "Desde entao: " + ", ".join(parts) + "."
