"""Syncs completion status back from Paperclip into the local Store
(issue #157).

Dispatch (#152) is currently one-way: a task confirmed and handed to a
real Paperclip agent never gets its local `TaskState` updated once the
agent actually finishes. `Scheduler.admit_task`'s dependency check
(`_dependencies_done`) requires a dependency's local state to be
literally `DONE` - so a plan with dependent tasks stalls forever after
its first layer, even though the real work genuinely completed. This
closes that loop: call `sync_dispatched_tasks` periodically (the
runtime's own poll loop, like its existing report-time tick) to notice
real completions and let already-blocked tasks progress.

Kept as its own module (like plan_confirmation.py) for the same reason:
orchestrator.py owns `run_daily_cycle` and already imports FROM
decisions.py, so a decisions.py-adjacent module importing back from
orchestrator.py would be circular.
"""
from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.github_client import GitHubClient
from orchestrator.models import TaskState
from orchestrator.orchestrator import DailyCycleResult, run_daily_cycle
from orchestrator.paperclip_ops import PaperclipSession, find_created_task_id, get_task_status
from orchestrator.persistence import Store
from orchestrator.plan_confirmation import admission_window_clock, resolve_company_id
from orchestrator.project_resolver import ProjectContext, ProjectResolver, ResolveError

# Paperclip issue statuses actually observed in production that mean
# "the agent is finished with this" - anything else (todo/blocked/
# in_progress/unrecognized) is left untouched. Never guesses at a status
# value this codebase hasn't verified against the real API.
_DONE_STATUSES = frozenset({"done"})

_LAST_SYNC_KEY = "orchestrator:last_paperclip_sync_at"


def sync_dispatched_tasks(
    store: Store, *, config: OrchestratorConfig | None = None,
    resolver: ProjectResolver | None = None, client: GitHubClient | None = None,
    paperclip_session_factory=None,
) -> dict:
    """Checks every locally READY/IN_PROGRESS task that has a project_id
    for a matching, already-dispatched Paperclip task (via
    `find_created_task_id` - never creates anything new), and marks it
    DONE locally when Paperclip reports it done. For every project that
    got at least one such completion, re-runs `run_daily_cycle` - the
    SAME already-reviewed admission+materialize+dispatch path #152 uses
    for a confirmed plan - so tasks that were blocked ONLY on that
    dependency admit and dispatch for real, without reimplementing any
    of that logic here.

    Never raises: every external call (project resolution, Paperclip
    status reads, GitHub/Paperclip dispatch) degrades to "skip this
    task/project" on failure, exactly like the daily cycle already does
    for its own callers. Returns a dict a caller can log or ignore -
    `synced_task_ids` (newly marked DONE) and `dispatch_results`
    (canonical_id -> DailyCycleResult, only for projects with a new
    completion this call)."""
    cfg = config or load_config()
    resolver = resolver or ProjectResolver()

    tracked = [
        task for task in store.list_tasks()
        if task.state in (TaskState.READY, TaskState.IN_PROGRESS) and task.project_id
    ]

    project_cache: dict[str, ProjectContext | None] = {}
    company_cache: dict[str, str | None] = {}

    def _project(canonical_id: str) -> ProjectContext | None:
        if canonical_id not in project_cache:
            resolved = resolver.resolve(canonical_id)
            project_cache[canonical_id] = None if isinstance(resolved, ResolveError) else resolved
        return project_cache[canonical_id]

    def _company(project: ProjectContext) -> str | None:
        if project.canonical_id not in company_cache:
            company_cache[project.canonical_id] = resolve_company_id(project, cfg)
        return company_cache[project.canonical_id]

    synced_task_ids: list[str] = []
    projects_with_new_completions: set[str] = set()

    for task in tracked:
        project = _project(task.project_id)
        if project is None:
            continue
        company_id = _company(project)
        if company_id is None:
            continue
        remote_id = find_created_task_id(company_id, task.correlation_id, store=store, config=cfg)
        if remote_id is None:
            continue
        status = get_task_status(company_id, remote_id, config=cfg)
        if not status.get("available"):
            continue
        remote_status = (status.get("status") or "").strip().lower()
        if remote_status not in _DONE_STATUSES:
            continue
        task.state = TaskState.DONE
        store.save_task(task)
        synced_task_ids.append(task.id)
        projects_with_new_completions.add(project.canonical_id)

    dispatch_results: dict[str, DailyCycleResult] = {}
    for canonical_id in projects_with_new_completions:
        project = _project(canonical_id)
        if project is None:
            continue
        clock = admission_window_clock(cfg)
        if clock is None:
            continue
        company_id = company_cache.get(canonical_id)
        session = None
        if company_id is not None:
            session = (paperclip_session_factory or PaperclipSession)(config=cfg)
        dispatch_results[canonical_id] = run_daily_cycle(
            store, project,
            client=client or GitHubClient(store, config=cfg),
            paperclip_session=session, company_id=company_id,
            clock=clock, config=cfg,
        )

    return {"synced_task_ids": synced_task_ids, "dispatch_results": dispatch_results}


def process_paperclip_sync_tick(
    store: Store, *, config: OrchestratorConfig | None = None, clock=None, **kwargs,
) -> dict | None:
    """Throttled entry point for the runtime loop: runs
    `sync_dispatched_tasks` at most once every
    `config.paperclip_sync_interval_seconds` (default 60s) - a poll every
    ~3s (the loop's own default cadence) would hammer Paperclip's API for
    no benefit, since real agent work takes minutes, not seconds. Returns
    None when it skipped this tick (too soon), or `sync_dispatched_tasks`'s
    own return dict when it actually ran. Never raises - a malformed
    stored timestamp is treated as "never ran", same fail-open stance as
    every other tick in this codebase."""
    cfg = config or load_config()
    now = clock() if clock else datetime.now(timezone.utc)
    raw = store.get_sync_value(_LAST_SYNC_KEY)
    if raw:
        try:
            last_run = datetime.fromisoformat(raw)
            if (now - last_run).total_seconds() < cfg.paperclip_sync_interval_seconds:
                return None
        except (TypeError, ValueError):
            pass
    store.set_sync_value(_LAST_SYNC_KEY, now.isoformat())
    return sync_dispatched_tasks(store, config=cfg, **kwargs)
