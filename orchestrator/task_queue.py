"""Dependency-local queue eligibility (#28), without global blocking.

Call get_promotable_tasks with a fresh Store.list_tasks() snapshot whenever a
task completes or a blocker is resolved. This function never changes states:
a dependency wait only excludes a task from the returned list, so it can become
eligible on the next snapshot without clearing a planner/human BLOCKED state.
The scheduler/runtime still applies cycle, cutoff and execution-agent policy.

Also holds materialize_plan (#23): the bridge from a planner.PlanResult to
the project's REAL task queue.
"""
from __future__ import annotations

import re
from pathlib import Path

from orchestrator.github_client import GitHubClient
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store

_QUEUED_STATES = {TaskState.PLANNED, TaskState.NEXT_CYCLE, TaskState.READY}


def get_promotable_tasks(all_tasks: list[Task]) -> list[Task]:
    """Return queued tasks whose own dependencies are all DONE, in input order.

    Missing, self and cyclic dependencies cannot satisfy the DONE requirement.
    A blocked ancestor keeps its queued descendants unavailable transitively:
    each descendant waits for its immediate predecessors to actually finish.
    Unblocking an ancestor alone does not count as completing it.

    Explicit BLOCKED/NEEDS_LUCAS, unplanned, active and terminal tasks are never
    returned. Duplicate IDs are rejected rather than choosing an arbitrary
    state that could incorrectly permit execution. The input is not mutated.
    """
    by_id: dict[str, Task] = {}
    for task in all_tasks:
        if task.id in by_id:
            raise ValueError(f"Duplicate task id in queue snapshot: {task.id}")
        by_id[task.id] = task

    return [
        task for task in all_tasks
        if task.state in _QUEUED_STATES
        and all(
            dependency != task.id
            and dependency in by_id
            and by_id[dependency].state == TaskState.DONE
            for dependency in task.dependencies
        )
    ]


_FALLBACK_PATH_PATTERN = re.compile(r"[\w./\\-]+\.(?:md|txt)", re.IGNORECASE)
_GITHUB_ISSUES_MENTION = re.compile(r"github[ _]issues", re.IGNORECASE)
# task_source is free, human-written prose (see project_context_index.json) -
# it can mention "GitHub Issues" while actively denying it applies here, e.g.
# "... Not GitHub Issues for this project." A same-sentence "not" nearby must
# override a bare substring match, or that sentence gets read backwards.
_GITHUB_ISSUES_NEGATED = re.compile(r"\bnot\b[^.]{0,30}github[ _]issues", re.IGNORECASE)


def _uses_github_issues(project_context) -> bool:
    """A resolvable project-owned queue file (e.g. FILA.md) is concrete
    evidence of the actual convention and always wins over a free-text
    mention of GitHub Issues, negated or not. Absent that, only an
    unnegated "GitHub Issues" mention counts."""
    source = project_context.task_source or ""
    if not source:
        return False
    if _resolve_fallback_path(project_context) is not None:
        return False
    if not _GITHUB_ISSUES_MENTION.search(source):
        return False
    return not _GITHUB_ISSUES_NEGATED.search(source)


def _resolve_fallback_path(project_context) -> Path | None:
    """Best-effort extraction of the project's own queue file from its
    (free-text, human-written) task_source description - e.g. 'docs\\ai\\
    FILA.md in this repo'. Returns None when no such path is found, OR
    when the extracted path would resolve outside project_context.root
    (a rooted/absolute fragment like '/etc/x.md', or a '../' escape) -
    this module never guesses a filename NOR writes anywhere the project
    didn't authorize, per #23's OUT OF SCOPE (never forces a migration/
    format the project didn't already establish, and never as a side
    door out of its own directory)."""
    if not project_context.root or not project_context.task_source:
        return None
    match = _FALLBACK_PATH_PATTERN.search(project_context.task_source)
    if not match:
        return None
    root = Path(project_context.root).resolve()
    candidate = (root / match.group(0).replace("\\", "/")).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _analyze_batch(tasks: list[Task]) -> tuple[list[Task], set[str]]:
    """Topologically orders `tasks` (Kahn's algorithm, dependencies within
    this batch only) and computes which ids must be DEFERRED - never
    materialized as free-standing work in either destination - because
    they are NEEDS_LUCAS, already BLOCKED (including a cycle the planner
    already flagged, #22), part of a cycle within this batch that ISN'T
    state-flagged (defensive - a hand-built plan might not have gone
    through the planner's own cycle detection), or depend directly or
    transitively on another deferred task in this same batch. A
    dependency OUTSIDE this batch (e.g. one collapsed onto an existing
    store task by the planner's own dedup) is never grounds for deferral
    - #23's scope only covers wiring dependencies among tasks
    materialized together; it just can't be referenced by a real Issue
    number created here."""
    by_id = {task.id: task for task in tasks}
    remaining_deps = {
        task.id: sum(1 for dep_id in task.dependencies if dep_id in by_id)
        for task in tasks
    }
    ready = [task for task in tasks if remaining_deps[task.id] == 0]
    ordered: list[Task] = []
    resolved: set[str] = set()

    while ready:
        task = ready.pop(0)
        if task.id in resolved:
            continue
        resolved.add(task.id)
        ordered.append(task)
        for other in tasks:
            if other.id in resolved or task.id not in other.dependencies:
                continue
            remaining_deps[other.id] -= 1
            if remaining_deps[other.id] == 0:
                ready.append(other)

    cyclic = [task for task in tasks if task.id not in resolved]
    ordered.extend(cyclic)

    deferred = {task.id for task in tasks if task.state in (TaskState.NEEDS_LUCAS, TaskState.BLOCKED)}
    deferred.update(task.id for task in cyclic)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            if task.id in deferred:
                continue
            if any(dep_id in by_id and dep_id in deferred for dep_id in task.dependencies):
                deferred.add(task.id)
                changed = True

    return ordered, deferred


def _issue_body(task: Task, blocked_by_numbers: list[int], blocks_numbers: list[int]) -> str:
    lines = [task.objective or task.title, ""]
    if task.acceptance_criteria:
        lines.append("## ACCEPTANCE CRITERIA")
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
        lines.append("")
    lines.append("## RELATIONSHIPS")
    lines.append(
        "BLOCKED_BY: " + (", ".join(f"#{number}" for number in blocked_by_numbers) if blocked_by_numbers else "none")
    )
    lines.append(
        "BLOCKS: " + (", ".join(f"#{number}" for number in blocks_numbers) if blocks_numbers else "none")
    )
    return "\n".join(lines)


def _materialize_to_github(
    tasks: list[Task], project_context, store: Store, client: GitHubClient | None,
) -> list[int]:
    if not project_context.repository:
        return []
    client = client or GitHubClient(store)
    by_id = {task.id: task for task in tasks}
    ordered, deferred = _analyze_batch(tasks)
    task_to_issue: dict[str, int] = {}
    created: list[int] = []

    for task in ordered:
        if task.id in deferred:
            continue
        unresolved_in_batch = [
            dep_id for dep_id in task.dependencies
            if dep_id in by_id and dep_id not in task_to_issue
        ]
        if unresolved_in_batch:
            # A dependency in THIS batch hasn't (yet, or ever) become a
            # real Issue - publishing this task now would show it as
            # free-standing (BLOCKED_BY: none) when it is not. Deferring
            # it also makes it unresolved for anything depending on IT,
            # cascading forward through however many levels apply.
            deferred.add(task.id)
            continue
        blocked_by_numbers = [task_to_issue[dep_id] for dep_id in task.dependencies if dep_id in task_to_issue]
        result = client.create_issue(
            project_context.repository, task.title, _issue_body(task, blocked_by_numbers, []),
            ["origin:planner"], task.correlation_id,
        )
        if result.get("available") and isinstance(result.get("number"), int):
            task_to_issue[task.id] = result["number"]
            created.append(result["number"])
        else:
            deferred.add(task.id)

    # Second pass: a child's Issue number cannot be known before it
    # exists, so BLOCKS can only be backfilled onto its parent's body
    # after every creation in this batch has settled. update_issue_body
    # always sends the full desired body, so repeating this is a no-op,
    # not a duplicate (#23, finding #4).
    blocks_by_parent: dict[str, list[int]] = {}
    for task in tasks:
        child_number = task_to_issue.get(task.id)
        if child_number is None:
            continue
        for dep_id in task.dependencies:
            parent_number = task_to_issue.get(dep_id)
            if parent_number is not None:
                blocks_by_parent.setdefault(dep_id, []).append(child_number)

    for parent_id, blocks_numbers in blocks_by_parent.items():
        parent = by_id[parent_id]
        blocked_by_numbers = [task_to_issue[dep_id] for dep_id in parent.dependencies if dep_id in task_to_issue]
        client.update_issue_body(
            project_context.repository, task_to_issue[parent_id],
            _issue_body(parent, blocked_by_numbers, sorted(blocks_numbers)),
        )

    return created


def _format_fallback_block(pending: list[Task], by_id: dict[str, Task]) -> str:
    lines = ["## Novas tarefas do planner", ""]
    for task in pending:
        dep_titles = [by_id[dep_id].title for dep_id in task.dependencies if dep_id in by_id]
        lines.append(f"- [ ] {task.title} (depende de: {', '.join(dep_titles) if dep_titles else 'nenhuma'})")
        if task.objective and task.objective != task.title:
            lines.append(f"  {task.objective}")
    return "\n".join(lines) + "\n"


def _materialize_to_fallback(tasks: list[Task], path: Path, store: Store) -> None:
    """Appends never-before-written tasks to the project's own queue file.

    Durability/idempotency uses #13's generic idempotency_keys table, one
    key per (task correlation_id, destination path): a task already
    marked written is skipped on a replay, and content already in the
    file is never touched or duplicated. The residual gap - a crash
    between the file write succeeding and the key being recorded - is not
    closed here: unlike #17/#18's network operations, a local file append
    has no "uncertain, might have landed" state to reconcile against, so
    the ordinary repeated-call case (the one actually reported, #23
    Review Task #117 finding #3) is what this guards.
    """
    by_id = {task.id: task for task in tasks}
    ordered, deferred = _analyze_batch(tasks)
    kind = f"fallback_queue:{path}"
    pending = [
        task for task in ordered
        if task.id not in deferred and not store.has_idempotency_key(task.correlation_id, kind)
    ]
    if not pending:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n" + _format_fallback_block(pending, by_id))
    except OSError:
        return
    for task in pending:
        store.record_idempotency_key(task.correlation_id, kind)


def materialize_plan(
    plan_result, project_context, store: Store, *, client: GitHubClient | None = None,
) -> list[int]:
    """Turns plan_result.tasks into the project's REAL task queue.

    Returns the GitHub Issue numbers created (in topological/creation
    order) when `project_context.task_source` indicates GitHub Issues is
    this project's queue (section 13). Each Issue's body records its
    BLOCKED_BY as real Issue numbers known at creation time, and gets its
    BLOCKS backfilled in a second pass once every creation in the batch
    has settled. Idempotency for a replay of the same plan comes from
    passing each Task's own stable correlation_id through to #17's
    GitHubClient.create_issue, which already dedupes on (repo,
    correlation_id) - this module adds none of its own for that part.

    A task that is NEEDS_LUCAS, already BLOCKED, part of an unresolved
    cycle within this batch, or depends (directly or transitively) on
    another deferred task is never materialized as if it were free
    (see _analyze_batch) - it is simply left for a later call once its
    blocker resolves, in EITHER destination.

    When the project does NOT use GitHub Issues, this never forces that
    convention on it (section 13): it appends a generic formatted text
    block to the project's own queue file, resolved from task_source (and
    verified to stay within project_context.root - see
    _resolve_fallback_path), and always returns `[]` (there is nothing to
    report as an Issue number). A project whose queue file can't be
    resolved from task_source simply gets no fallback write - migrating
    it onto a new format is out of scope.
    """
    tasks = list(plan_result.tasks)
    if not tasks:
        return []

    if _uses_github_issues(project_context):
        return _materialize_to_github(tasks, project_context, store, client)

    path = _resolve_fallback_path(project_context)
    if path is not None:
        _materialize_to_fallback(tasks, path, store)
    return []
