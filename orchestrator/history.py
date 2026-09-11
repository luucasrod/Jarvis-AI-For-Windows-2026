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
)

# BLOCKED isn't a distinct EventType in #14's list - a task being blocked
# is inferred from DECISION_REQUIRED (NEEDS_LUCAS) events for now. If a
# dedicated event is added later, this mapping is the one place to update.
_BLOCKED_PROXY_EVENT = EventType.DECISION_REQUIRED


@dataclass
class HistorySummary:
    since: datetime
    tasks_created: int = 0
    tasks_completed: int = 0
    tasks_blocked: int = 0
    reviews_passed: int = 0
    reviews_failed: int = 0
    bugs_found: int = 0
    deployments_finished: int = 0
    by_project: dict[str, dict[str, int]] = field(default_factory=dict)
    total_events: int = 0


def diff_since(store: Store, since: datetime) -> HistorySummary:
    events = query_events(store, since=since, event_types=list(_COUNTED_EVENT_TYPES))
    summary = HistorySummary(since=since, total_events=len(events))

    for event in events:
        event_type = event["event_type"]
        project_id = event["project_id"] or "sem_projeto"

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
        elif event_type == _BLOCKED_PROXY_EVENT:
            summary.tasks_blocked += 1

    return summary


def summarize_for_voice(summary: HistorySummary) -> str:
    if summary.total_events == 0:
        return "Nada relevante aconteceu nesse periodo."

    parts = []
    if summary.tasks_created:
        parts.append(f"{summary.tasks_created} tarefa(s) criada(s)")
    if summary.tasks_completed:
        parts.append(f"{summary.tasks_completed} concluida(s)")
    if summary.tasks_blocked:
        parts.append(f"{summary.tasks_blocked} bloqueada(s) esperando decisao")
    if summary.reviews_passed:
        parts.append(f"{summary.reviews_passed} revisao(oes) aprovada(s)")
    if summary.reviews_failed:
        parts.append(f"{summary.reviews_failed} revisao(oes) reprovada(s)")
    if summary.bugs_found:
        parts.append(f"{summary.bugs_found} bug(s) encontrado(s)")
    if summary.deployments_finished:
        parts.append(f"{summary.deployments_finished} deploy(s) concluido(s)")

    if not parts:
        return "Alguma atividade registrada, mas nada nas categorias principais."

    return "Desde entao: " + ", ".join(parts) + "."
