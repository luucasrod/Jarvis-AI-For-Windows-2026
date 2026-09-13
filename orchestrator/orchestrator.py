"""Wiring for the daily cycle (issue #30): the composition point that
connects the Scheduler (#25), the queue-eligibility/materialize bridge
(#23/#28), agent availability/priority (#26/#31), Paperclip (#18) and
GitHub (#17) into the actual flow described in PROMPT MESTRE V2 sections 4
and 15-17, even before Telegram credentials (#19/#31) are configured.

Deliberately a thin composition layer: every piece of real logic (which
tasks are promotable, how to materialize them, whether an agent is free,
whether the queue looks idle) already exists and is independently tested
elsewhere. This module only sequences those calls the way one daily cycle
actually needs them called, and assembles their results into one summary
the caller (the Scheduler's own runtime loop, eventually) can act on or log.

No PARALLEL_SAFE concern here (the issue marks this SOLO) - every call
below already carries its own idempotency (create_issue's correlation_id
dedup, create_task_idempotent's operation key, run_sync_once for the
scheduler ticks), so running the same cycle twice (a retry after a crash,
or a manual re-run) never duplicates GitHub Issues or Paperclip tasks.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import paperclip_client
from orchestrator.agent_availability import is_agent_available
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.decisions import get_priority_queue
from orchestrator.events import EventType, query_events
from orchestrator.github_client import GitHubClient
from orchestrator.healthcheck import IdleDiagnosis, check_idle
from orchestrator.history import diff_since
from orchestrator.models import AgentName, TaskState
from orchestrator.paperclip_ops import PaperclipSession
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult
from orchestrator.scheduler import Scheduler
from orchestrator.task_queue import materialize_plan

_LAST_REPORT_KEY = "orchestrator:last_report_at"
_CONCRETE_AGENTS = (AgentName.CLAUDE, AgentName.CODEX)


@dataclass
class DailyCycleResult:
    cycle_fired: bool
    promoted_task_ids: list[str] = field(default_factory=list)
    ready_task_ids: list[str] = field(default_factory=list)
    created_issue_numbers: list[int] = field(default_factory=list)
    paperclip_created_task_ids: list[str] = field(default_factory=list)
    assigned_task_ids: list[str] = field(default_factory=list)
    dispatch_incomplete_task_ids: list[str] = field(default_factory=list)
    idle_diagnosis: IdleDiagnosis | None = None


def _concrete_available_agent(store: Store, task, clock) -> AgentName | None:
    """Returns a concrete (CLAUDE/CODEX) agent free to take `task` right
    now, `AgentName.EITHER` when the task names no concrete candidate at
    all (nothing for #26 to check), or `None` when every concrete
    candidate it DOES name (preferred and/or fallback) is in cooldown -
    #30 never creates Paperclip work for a task with no free target
    (Review Task #131 finding #3)."""
    candidates: list[AgentName] = []
    if task.preferred_agent in _CONCRETE_AGENTS:
        candidates.append(task.preferred_agent)
    if task.fallback_agent in _CONCRETE_AGENTS and task.fallback_agent not in candidates:
        candidates.append(task.fallback_agent)
    if not candidates:
        return AgentName.EITHER
    for agent in candidates:
        if is_agent_available(store, agent, clock=clock):
            return agent
    return None


def run_daily_cycle(
    store: Store,
    project_context,
    *,
    client: GitHubClient | None = None,
    paperclip_session: PaperclipSession | None = None,
    company_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
    config: OrchestratorConfig | None = None,
    paperclip_available: Callable[[], bool] | None = None,
    paperclip_snapshot: Callable[[], dict] | None = None,
) -> DailyCycleResult:
    """Runs one daily-cycle pass for `project_context` specifically:
    reconsiders eligible queued tasks through the Scheduler's own
    admission (never bypassing its window/guard), then gives every
    currently-READY task belonging to THIS project real work in
    Paperclip/GitHub, in priority order, only for a task with a free
    concrete agent.

    Promotion (Review Task #131 finding #1): `scheduler.on_cycle_start()`
    promotes NEXT_CYCLE tasks whose dependencies are DONE once per local
    day, transactionally, respecting the admission window/guard. PLANNED
    tasks whose dependency finishes LATER are never revisited by that
    call, so each is individually re-admitted via `scheduler.admit_task`
    - the SAME transactional, window-aware primitive, never a direct
    `task.state = READY` write. A direct write here previously bypassed
    the window entirely (reproduced promoting at 07:00/16:00/after a
    persisted cutoff with the clock moved back) and clobbered concurrent
    writes via a stale snapshot. `promoted_task_ids` is derived from the
    TASK_READY events actually emitted during this call - by scheduler
    admission or this loop, whichever fired first for a given task -
    rather than tracked separately, so a promotion the scheduler itself
    performs is never silently omitted from the result.

    Project scoping (finding #2): only READY tasks whose `project_id`
    matches `project_context.canonical_id` are ever materialized/
    dispatched here. The scheduler's own tick can stay global (its
    contract, not #30's), but THIS call never publishes another
    project's task into `project_context`'s repository/company_id.

    Eligibility and order (finding #3): the dispatch set is filtered
    through `get_priority_queue` (#31, itself built on #28's own
    dependency-DONE check) rather than a raw `list_tasks(READY)` scan, so
    a READY task with a since-invalidated/missing dependency is never
    dispatched, and urgent/high work is handed out before low/medium.

    Assignment (finding #4): a Paperclip task is only created for a task
    with a free concrete agent (`_concrete_available_agent`); its real
    Paperclip agent id is resolved via `paperclip_client.find_agent` and
    passed as `assignee_agent_id`, and the assignment is INDEPENDENTLY
    confirmed afterward via `get_task_status` - a created-but-unassigned
    (or unresolvable-agent) task is reported in
    `dispatch_incomplete_task_ids`, never silently counted as progress.
    `check_idle`'s later diagnosis does not substitute for this: its own
    grace period can hide exactly this kind of immediate dispatch
    failure. `paperclip_client.find_agent`/`resume_agent` exist, but
    resuming a deliberately paused agent to force an assignment is out of
    scope - no such endpoint is invented here.

    GitHub materialization (finding #5): `materialize_plan` is now always
    called for this project's dispatchable tasks (never gated behind
    `client is not None`) - it already accepts an optional client and
    decides the real destination (GitHub Issues vs the project's own
    queue file) itself; gating it here silently dropped a FILA.md-only
    project's work entirely.
    """
    config = config or load_config()
    scheduler = Scheduler(store, clock=clock, config=config)
    cycle_started_at = datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc)

    cycle_fired = scheduler.on_cycle_start()

    for task in get_priority_queue(store):
        if task.state == TaskState.PLANNED:
            scheduler.admit_task(task)

    promoted_ids = [
        event["payload"]["task_id"]
        for event in query_events(store, since=cycle_started_at, event_types=[EventType.TASK_READY])
        if isinstance(event.get("payload"), dict) and "task_id" in event["payload"]
    ]

    dispatchable = [
        task for task in get_priority_queue(store)
        if task.state == TaskState.READY and task.project_id == project_context.canonical_id
    ]

    created_issue_numbers: list[int] = []
    if dispatchable:
        created_issue_numbers = materialize_plan(
            PlanResult(tasks=dispatchable), project_context, store, client=client,
        )

    paperclip_created: list[str] = []
    assigned: list[str] = []
    dispatch_incomplete: list[str] = []
    if paperclip_session is not None and company_id is not None:
        for task in dispatchable:
            resolved_agent = _concrete_available_agent(store, task, clock)
            if resolved_agent is None:
                continue  # every concrete candidate is in cooldown - #26

            agent_id = None
            if resolved_agent is not AgentName.EITHER:
                agent_info, _agent_error = paperclip_client.find_agent(resolved_agent.value)
                agent_id = agent_info.get("id") if isinstance(agent_info, dict) else None

            result = paperclip_session.create_task_idempotent(
                company_id, task.title, task.objective, task.correlation_id, agent_id, store=store,
            )
            if not result.get("available"):
                dispatch_incomplete.append(task.id)
                continue
            paperclip_created.append(task.id)

            status = paperclip_session.get_task_status(company_id, result["task_id"])
            confirmed_assignee = (
                status.get("available") and isinstance(status.get("task"), dict)
                and status["task"].get("assigneeAgentId")
            )
            if confirmed_assignee:
                assigned.append(task.id)
            else:
                dispatch_incomplete.append(task.id)

    diagnosis = check_idle(
        store, config=config, clock=clock,
        paperclip_available=paperclip_available, paperclip_snapshot=paperclip_snapshot,
    )

    return DailyCycleResult(
        cycle_fired=cycle_fired,
        promoted_task_ids=promoted_ids,
        ready_task_ids=[task.id for task in dispatchable],
        created_issue_numbers=created_issue_numbers,
        paperclip_created_task_ids=paperclip_created,
        assigned_task_ids=assigned,
        dispatch_incomplete_task_ids=dispatch_incomplete,
        idle_diagnosis=diagnosis,
    )


def run_cutoff(
    store: Store, *, clock: Callable[[], datetime] | None = None,
    config: OrchestratorConfig | None = None,
) -> bool:
    """Closes today's admission window (#25's own `on_cutoff`): new/
    reconsidered tasks fall back to NEXT_CYCLE instead of READY from this
    point on. Never touches IN_PROGRESS tasks - `on_cutoff` only records a
    sync-state guard, it does not read or write the `tasks` table at all."""
    return Scheduler(store, clock=clock, config=config).on_cutoff()


def run_report(
    store: Store, *, since: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    """Assembles the RAW data for the daily report - counts and task ids
    only, no Telegram/voice formatting (that stays #31/#19's job) and no
    send confirmation (that stays #32's job, per Review Task #131 finding
    #6).

    `since` defaults to the LAST CONFIRMED report cursor
    (`mark_report_delivered`), falling back to 24h before `now` the very
    first time it is ever called. This call itself is a PURE READ - it
    never advances that cursor. The previous version wrote the cursor on
    every call, so a second collection for the same report window (e.g.
    the first collection's send failed downstream, before #32 existed to
    confirm it) silently saw an empty window and under-reported. Whoever
    actually confirms delivery must call `mark_report_delivered` once
    that succeeds - #30 does not send, so it cannot know when that is.
    """
    now = datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc)
    if since is None:
        stored = store.get_sync_value(_LAST_REPORT_KEY)
        since = datetime.fromisoformat(stored) if stored else now - timedelta(hours=24)

    summary = diff_since(store, since)
    return {
        "since": since,
        "generated_at": now,
        "tasks_completed": summary.tasks_completed,
        "tasks_in_progress": [task.id for task in store.list_tasks(TaskState.IN_PROGRESS)],
        "merges_completed": summary.merges_completed,
        "deployments_finished": summary.deployments_finished,
        "bugs_found": summary.bugs_found,
        "tasks_blocked_since": summary.tasks_blocked,
        "tasks_currently_blocked": [task.id for task in store.list_tasks(TaskState.BLOCKED)],
        "needs_lucas": [task.id for task in store.list_tasks(TaskState.NEEDS_LUCAS)],
        "decisions_pending": summary.decisions_pending,
        "next_cycle": [task.id for task in store.list_tasks(TaskState.NEXT_CYCLE)],
    }


def mark_report_delivered(
    store: Store, *, at: datetime | None = None, clock: Callable[[], datetime] | None = None,
) -> None:
    """Advances the report cursor `run_report` defaults `since` from.
    Called by whoever actually confirms delivery (#32) once it succeeds -
    never automatically by `run_report` itself (see its own docstring)."""
    now = at or (datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc))
    store.set_sync_value(_LAST_REPORT_KEY, now.isoformat())
