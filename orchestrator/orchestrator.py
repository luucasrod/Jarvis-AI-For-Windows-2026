"""Wiring for the daily cycle (issue #30): the composition point that
connects the Scheduler (#25), the queue-eligibility/materialize bridge
(#23/#28), Paperclip (#18) and GitHub (#17) into the actual flow described
in PROMPT MESTRE V2 sections 4 and 15-17, even before Telegram credentials
(#19/#31) are configured.

Deliberately a thin composition layer: every piece of real logic (which
tasks are promotable, how to materialize them, whether the queue looks
idle) already exists and is independently tested elsewhere. This module
only sequences those calls the way one daily cycle actually needs them
called, and assembles their results into one summary the caller (the
Scheduler's own runtime loop, eventually) can act on or log.

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

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.github_client import GitHubClient
from orchestrator.healthcheck import IdleDiagnosis, check_idle
from orchestrator.history import diff_since
from orchestrator.models import TaskState
from orchestrator.paperclip_ops import PaperclipSession
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult
from orchestrator.scheduler import Scheduler
from orchestrator.task_queue import get_promotable_tasks, materialize_plan

_LAST_REPORT_KEY = "orchestrator:last_report_at"


@dataclass
class DailyCycleResult:
    cycle_fired: bool
    promoted_task_ids: list[str] = field(default_factory=list)
    ready_task_ids: list[str] = field(default_factory=list)
    created_issue_numbers: list[int] = field(default_factory=list)
    paperclip_created_task_ids: list[str] = field(default_factory=list)
    idle_diagnosis: IdleDiagnosis | None = None


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
    """Runs one daily-cycle pass: promotes eligible queued tasks, gives
    every currently-READY task real work in Paperclip/GitHub, and reports
    whether the queue looks stuck afterward.

    `scheduler.on_cycle_start()` already promotes NEXT_CYCLE tasks whose
    dependencies are DONE once per local day (idempotent via
    run_sync_once) - this is called first so its own bookkeeping (the
    TASK_READY event, the once-per-day guard) stays authoritative for
    that specific transition. `get_promotable_tasks` is then re-applied
    across the WHOLE queue (including PLANNED tasks the scheduler's own
    admission never revisits - #28's actual reason to exist) so a task
    whose dependency finished sometime AFTER it was first admitted still
    gets promoted here rather than waiting for its own next admission.
    Reassessing "priority" beyond dependency-eligibility is out of scope:
    no other module in this codebase orders the queue by priority yet, so
    `get_promotable_tasks`'s own input-order is the only ordering applied.

    Every task that ends this pass in READY (whether promoted just now or
    already READY from an earlier cycle) is handed to `materialize_plan`
    (batched as one PlanResult) so any that never got a GitHub Issue yet
    do, and to `paperclip_session.create_task_idempotent` (when a session
    and `company_id` are supplied) so it has a real assignee-facing task.
    Both are naturally idempotent per task, so re-running this on
    already-materialized READY tasks is a no-op for them.

    `check_idle` (#27) runs last and its diagnosis is returned rather than
    acted on here - #30 does not decide what to DO about idleness, only
    surfaces whether the cycle actually produced observable progress.
    """
    config = config or load_config()
    scheduler = Scheduler(store, clock=clock, config=config)
    cycle_fired = scheduler.on_cycle_start()

    now = datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc)
    promoted_ids: list[str] = []
    for task in get_promotable_tasks(store.list_tasks()):
        if task.state == TaskState.READY:
            continue
        task.state = TaskState.READY
        task.updated_at = now
        store.save_task(task)
        promoted_ids.append(task.id)

    ready_tasks = store.list_tasks(TaskState.READY)

    created_issue_numbers: list[int] = []
    if client is not None and ready_tasks:
        created_issue_numbers = materialize_plan(
            PlanResult(tasks=ready_tasks), project_context, store, client=client,
        )

    paperclip_created: list[str] = []
    if paperclip_session is not None and company_id is not None:
        for task in ready_tasks:
            result = paperclip_session.create_task_idempotent(
                company_id, task.title, task.objective, task.correlation_id, store=store,
            )
            if result.get("available"):
                paperclip_created.append(task.id)

    diagnosis = check_idle(
        store, config=config, clock=clock,
        paperclip_available=paperclip_available, paperclip_snapshot=paperclip_snapshot,
    )

    return DailyCycleResult(
        cycle_fired=cycle_fired,
        promoted_task_ids=promoted_ids,
        ready_task_ids=[task.id for task in ready_tasks],
        created_issue_numbers=created_issue_numbers,
        paperclip_created_task_ids=paperclip_created,
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
    only, no Telegram/voice formatting (that stays #31/#19's job).

    `since` defaults to this store's own last `run_report` call (tracked
    under `_LAST_REPORT_KEY`), falling back to 24h before `now` the very
    first time it is ever called - #30 does not assume the Scheduler's
    own `report_time` tick fired immediately before this, since a report
    can also be requested on demand.
    """
    now = datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc)
    if since is None:
        stored = store.get_sync_value(_LAST_REPORT_KEY)
        since = datetime.fromisoformat(stored) if stored else now - timedelta(hours=24)

    summary = diff_since(store, since)
    report = {
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
    store.set_sync_value(_LAST_REPORT_KEY, now.isoformat())
    return report
