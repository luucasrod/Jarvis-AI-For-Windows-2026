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

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import paperclip_client
from orchestrator.agent_availability import is_agent_available
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.decisions import get_priority_queue
from orchestrator.github_client import GitHubClient
from orchestrator.healthcheck import IdleDiagnosis, check_idle
from orchestrator.history import diff_since
from orchestrator.models import AgentName, Task, TaskState
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
    now, or `None` when no concrete agent is free.

    A task naming a concrete preferred/fallback tries those specifically,
    in order. A task naming NEITHER (preferred_agent EITHER/NONE with no
    concrete fallback) still requires an ACTUAL free agent among both -
    #26's cooldown state is never skipped just because the task itself
    didn't name a candidate (Review Task #131 round 2, finding #1: the
    previous version returned a sentinel here and let dispatch proceed
    with assignee=None even with both Claude and Codex in cooldown)."""
    candidates: list[AgentName] = []
    if task.preferred_agent in _CONCRETE_AGENTS:
        candidates.append(task.preferred_agent)
    if task.fallback_agent in _CONCRETE_AGENTS and task.fallback_agent not in candidates:
        candidates.append(task.fallback_agent)
    if not candidates:
        candidates = list(_CONCRETE_AGENTS)
    for agent in candidates:
        if is_agent_available(store, agent, clock=clock):
            return agent
    return None


def _resolve_agent_id(name: str, company_id: str, session: PaperclipSession) -> str | None:
    """Resolves `name` ("Claude"/"Codex") to a concrete Paperclip agent id
    WITHIN `company_id` specifically, on the SAME server `session` itself
    talks to. `find_agent` defaults to searching every company and
    accepting a substring match - passing `company_id` restricts it to
    this one, and `session.config`'s own base_url/timeout keep this
    lookup on the exact server the rest of dispatch uses (Review Task
    #131 round 2, finding #2: a bare `find_agent(name)` call could return
    a same-named agent from a DIFFERENT company, and used whatever
    server this module's own global config happened to point at)."""
    agent_info, error = paperclip_client.find_agent(
        name, company_id=company_id,
        base_url=session.config.paperclip_base_url, timeout=session.config.paperclip_timeout_seconds,
    )
    if error is not None or not isinstance(agent_info, dict):
        return None
    # Independently re-verified rather than only trusted from the
    # `company_id` argument passed above - defense in depth against a
    # `find_agent` that doesn't actually scope its own search (an older
    # version, or a future regression) silently handing back a same-named
    # agent from a different company.
    if agent_info.get("_company_id") != company_id:
        return None
    agent_id = agent_info.get("id")
    return agent_id if isinstance(agent_id, str) and agent_id else None


_DISPATCH_INTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS orchestrator_dispatch_intent (
    server TEXT NOT NULL,
    company_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    implementer TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY (server, company_id, correlation_id)
);
"""


def _dispatch_server(session: PaperclipSession) -> str:
    """Normalizes the session's own base_url the SAME way #18 does for its
    own fingerprint (`.rstrip('/')`) - the server is part of a dispatch
    intent's identity (Review Task #131 round 4, finding #1): reusing an
    agent id resolved on one Paperclip server against a DIFFERENT one
    (e.g. this Store/task/company reused across a server migration) would
    silently send an id that was never looked up there at all."""
    return session.config.paperclip_base_url.rstrip('/')


def _load_dispatch_intent(
    store: Store, server: str, company_id: str, correlation_id: str,
) -> tuple[str, str, str] | None:
    """Returns the (agent_id, title, description) already committed to for
    this dispatch on THIS server, if any - see `_reserve_dispatch_intent`."""
    store.ensure_schema(_DISPATCH_INTENT_SCHEMA)
    rows = store.query(
        "SELECT agent_id, title, description FROM orchestrator_dispatch_intent "
        "WHERE server = ? AND company_id = ? AND correlation_id = ?",
        (server, company_id, correlation_id),
    )
    return rows[0] if rows else None


def _reserve_dispatch_intent(
    store: Store, server: str, company_id: str, task_id: str, project_context, implementer: AgentName,
    agent_id: str, title: str, description: str, now: datetime,
) -> tuple[str, str, str] | None:
    """Commits ONE stable dispatch identity (server + agent + exact title/
    description) for this task, the first and only time it is ever
    decided, transactionally re-validating the task is STILL genuinely
    eligible right before that commit - closing findings from rounds 3
    and 4:

    Round 3 finding #1 (association incomplete for recovery): the
    previous design only remembered an agent id AFTER #18's own create
    confirmed it, so a POST whose response was lost (timeout) left
    nothing to reconcile from, and a retry - now resolving a DIFFERENT
    agent because the original one had since gone into cooldown, or
    using an in-the-meantime-edited local title - built a DIFFERENT
    fingerprint and hit #18's own `correlation_conflict` instead of
    reconciling. `orchestrator_dispatch_intent` is written BEFORE the
    first `create_task_idempotent` attempt and reused VERBATIM on every
    later call for this (server, company_id, correlation_id) (see
    `_load_dispatch_intent`) - #18's own reconciliation (querying the
    remote by marker) then naturally resolves an uncertain outcome, since
    the fingerprint never changes. A task with an existing intent is
    dispatched again with NO renewed availability requirement - the
    decision was already made; only a truly NEW dispatch decision needs a
    free agent.

    Round 3 finding #2 (stale snapshot dispatched, reviewer clobbered on
    active work): this module's outer `dispatchable` list is a snapshot
    that can go stale during a slow external call (e.g. `find_agent`)
    elsewhere in the same pass, while a concurrent writer moves the task
    on (a review started, a human edit). The current row is re-read fresh
    here, and dispatch is refused (returns None, no mutation, no intent
    written) unless it is STILL READY, in THIS project, with every
    dependency STILL DONE.

    Round 4 finding #1: `server` is now part of the intent's own identity
    (see `_dispatch_server`) - a Store/task/company reused against a
    different Paperclip server never inherits an id resolved on the old
    one.

    Round 4 finding #2 (losing reservation could still force self-review):
    two concurrent callers resolving DIFFERENT candidate implementers for
    the same never-yet-reserved correlation_id could previously each
    apply their OWN reviewer fixup - `run_in_transaction`'s BEGIN
    IMMEDIATE genuinely serializes the two attempts, so the loser's
    transaction runs strictly after the winner's already committed; an
    EXISTING intent is now always read FIRST, before any reviewer
    mutation, and a loser fixes up against the WINNING intent's own
    persisted `implementer` - never its own losing candidate. Only the
    actual winner (no existing intent found) ever picks an implementer at
    all.
    """
    def apply(connection):
        row = connection.execute("SELECT data FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        current = Task.from_dict(json.loads(row[0]))
        if current.state != TaskState.READY or current.project_id != project_context.canonical_id:
            return None
        for dep_id in current.dependencies:
            dep_row = connection.execute("SELECT state FROM tasks WHERE id = ?", (dep_id,)).fetchone()
            if dep_row is None or dep_row[0] != TaskState.DONE.value:
                return None

        existing = connection.execute(
            "SELECT agent_id, title, description, implementer FROM orchestrator_dispatch_intent "
            "WHERE server = ? AND company_id = ? AND correlation_id = ?",
            (server, company_id, current.correlation_id),
        ).fetchone()
        if existing is not None:
            won_implementer = AgentName(existing[3])
            if current.reviewer_preference == won_implementer:
                current.reviewer_preference = (
                    AgentName.CODEX if won_implementer == AgentName.CLAUDE else AgentName.CLAUDE
                )
                current.updated_at = now
                connection.execute("UPDATE tasks SET data = ? WHERE id = ?", (json.dumps(current.to_dict()), task_id))
            return existing[0], existing[1], existing[2]

        if current.reviewer_preference == implementer:
            current.reviewer_preference = AgentName.CODEX if implementer == AgentName.CLAUDE else AgentName.CLAUDE
            current.updated_at = now
            connection.execute("UPDATE tasks SET data = ? WHERE id = ?", (json.dumps(current.to_dict()), task_id))
        connection.execute(
            "INSERT OR IGNORE INTO orchestrator_dispatch_intent "
            "(server, company_id, correlation_id, agent_id, implementer, title, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (server, company_id, current.correlation_id, agent_id, implementer.value, title, description),
        )
        return connection.execute(
            "SELECT agent_id, title, description FROM orchestrator_dispatch_intent "
            "WHERE server = ? AND company_id = ? AND correlation_id = ?",
            (server, company_id, current.correlation_id),
        ).fetchone()

    store.ensure_schema(_DISPATCH_INTENT_SCHEMA)
    return store.run_in_transaction(apply)


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
    concrete agent and a resolvable, same-company Paperclip identity.

    Promotion (Review Task #131 findings #1 and #4): `on_cycle_start`
    promotes NEXT_CYCLE tasks whose dependencies are DONE once per local
    day, transactionally, respecting the admission window/guard - but
    only once: a NEXT_CYCLE task whose dependency finishes LATER that
    same day is never revisited by that one-shot scan. PLANNED tasks are
    individually re-admitted via `scheduler.admit_task`, and NEXT_CYCLE
    tasks via the new `scheduler.reconsider` (added for this) - both the
    SAME transactional, window-aware primitives, never a direct
    `task.state = READY` write (a direct write previously bypassed the
    window entirely and clobbered concurrent writes via a stale
    snapshot). `promoted_task_ids` is computed as the set difference
    between the READY tasks before and after this pass - round 2 found
    that deriving it from TASK_READY events `since=cycle_started_at`
    broke under a fixed/rolled-back clock (two calls at the identical
    instant re-counted the first call's own promotions); a plain
    before/after state diff has no such timestamp dependency.

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

    Assignment (round 2 findings #1-#3, round 3 findings #1-#2): a
    Paperclip task is only created for a task with a REAL free concrete
    agent (`_concrete_available_agent`, which checks actual availability
    even for a task naming no preference at all). Its Paperclip agent id
    is resolved via `_resolve_agent_id` - scoped to `company_id` and to
    the session's own server, never a same-named agent from a different
    company. That decision (agent id + the EXACT title/description used)
    is committed exactly once as a durable `orchestrator_dispatch_intent`
    row (`_reserve_dispatch_intent`) and reused VERBATIM on every later
    call for the same correlation_id (`_load_dispatch_intent`) - never
    re-decided. This closes two things at once: #18's own create is
    idempotent by a fingerprint that includes title/description/assignee,
    so re-deciding on a retry (a different agent because the original
    went into cooldown, or a locally-edited title) built a DIFFERENT
    fingerprint and hit `correlation_conflict` instead of reconciling; and
    a POST whose response was lost (timeout) left nothing to reconcile
    from until the SAME fingerprint was retried, which a fresh decision
    would never reproduce. Reassignment to a different agent stays a
    distinct, unimplemented operation. `_reserve_dispatch_intent` commits
    only after re-validating - inside the SAME transaction - that the
    task is STILL READY, still in this project, with every dependency
    STILL DONE, and only then fixes up a `task.reviewer_preference`
    collision (#24/#29's cross-review requirement): a stale outer
    `dispatchable` snapshot can otherwise go stale during a slow external
    call elsewhere in this same pass, letting a concurrent writer move
    the task on (e.g. into an active review) before this commits - the
    fresh re-check refuses dispatch (and any mutation) for a task that
    is no longer genuinely eligible, rather than acting on the stale
    snapshot. The assignment is INDEPENDENTLY confirmed afterward via
    `get_task_status`, comparing against the SPECIFIC id just used (not
    any non-empty value) - a task with no resolvable identity, no longer
    eligible, or created but not confirmed with THIS id, is reported in
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
    now = datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc)

    ready_before = {task.id for task in store.list_tasks(TaskState.READY)}
    cycle_fired = scheduler.on_cycle_start()

    for task in get_priority_queue(store):
        if task.state == TaskState.PLANNED:
            scheduler.admit_task(task)
        elif task.state == TaskState.NEXT_CYCLE:
            scheduler.reconsider(task)

    ready_after = {task.id for task in store.list_tasks(TaskState.READY)}
    promoted_ids = list(ready_after - ready_before)

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
        server = _dispatch_server(paperclip_session)
        for task in dispatchable:
            # An EXISTING intent is authoritative and reused VERBATIM,
            # with no renewed availability requirement - the decision was
            # already made once; only a genuinely NEW dispatch needs a
            # free agent (Review Task #131 round 3, finding #1).
            intent = _load_dispatch_intent(store, server, company_id, task.correlation_id)
            if intent is None:
                resolved_agent = _concrete_available_agent(store, task, clock)
                if resolved_agent is None:
                    dispatch_incomplete.append(task.id)  # every concrete candidate is in cooldown - #26
                    continue
                agent_id = _resolve_agent_id(resolved_agent.value, company_id, paperclip_session)
                if agent_id is None:
                    dispatch_incomplete.append(task.id)  # no valid, same-company identity resolvable
                    continue
                intent = _reserve_dispatch_intent(
                    store, server, company_id, task.id, project_context, resolved_agent,
                    agent_id, task.title, task.objective, now,
                )
                if intent is None:
                    dispatch_incomplete.append(task.id)  # no longer eligible - concurrent change
                    continue

            agent_id, title, description = intent
            result = paperclip_session.create_task_idempotent(
                company_id, title, description, task.correlation_id, agent_id, store=store,
            )
            if not result.get("available"):
                dispatch_incomplete.append(task.id)
                continue
            paperclip_created.append(task.id)

            status = paperclip_session.get_task_status(company_id, result["task_id"])
            confirmed_assignee = (
                status.get("available") and isinstance(status.get("task"), dict)
                and status["task"].get("assigneeAgentId") == agent_id
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
    never automatically by `run_report` itself (see its own docstring).

    Pass `at=report["generated_at"]` (the delivered snapshot's OWN
    instant) rather than the later time delivery actually happened - the
    cursor marks how much of the queue's history that snapshot covered,
    not when it left the building."""
    now = at or (datetime.now(timezone.utc) if clock is None else clock().astimezone(timezone.utc))
    store.set_sync_value(_LAST_REPORT_KEY, now.isoformat())
