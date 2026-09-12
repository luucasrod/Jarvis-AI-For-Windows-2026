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
    FILA.md in this repo'. Returns None when no such path is found; this
    module never guesses a filename, per #23's OUT OF SCOPE (never forces
    a migration/format the project didn't already establish)."""
    if not project_context.root or not project_context.task_source:
        return None
    match = _FALLBACK_PATH_PATTERN.search(project_context.task_source)
    if not match:
        return None
    return Path(project_context.root) / match.group(0).replace("\\", "/")


def _topological_order(tasks: list[Task]) -> list[Task]:
    """Kahn's algorithm restricted to dependencies within `tasks` - a
    dependency on a task outside this batch (e.g. one collapsed onto an
    existing store task by the planner's own dedup) is already resolved
    as far as ordering THIS batch goes; it just can't be referenced by a
    real Issue number created here (#23's own scope only covers wiring
    dependencies among tasks materialized together). Any leftover cycle
    - shouldn't happen, the planner already flags cycles as BLOCKED (#22)
    - is appended in original order rather than silently dropped."""
    by_id = {task.id: task for task in tasks}
    remaining_deps = {
        task.id: sum(1 for dep_id in task.dependencies if dep_id in by_id)
        for task in tasks
    }
    ready = [task for task in tasks if remaining_deps[task.id] == 0]
    ordered: list[Task] = []
    seen: set[str] = set()

    while ready:
        task = ready.pop(0)
        if task.id in seen:
            continue
        seen.add(task.id)
        ordered.append(task)
        for other in tasks:
            if other.id in seen or task.id not in other.dependencies:
                continue
            remaining_deps[other.id] -= 1
            if remaining_deps[other.id] == 0:
                ready.append(other)

    ordered.extend(task for task in tasks if task.id not in seen)
    return ordered


def _issue_body(task: Task, blocked_by_numbers: list[int]) -> str:
    lines = [task.objective or task.title, ""]
    if task.acceptance_criteria:
        lines.append("## ACCEPTANCE CRITERIA")
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
        lines.append("")
    lines.append("## RELATIONSHIPS")
    lines.append(
        "BLOCKED_BY: " + (", ".join(f"#{number}" for number in blocked_by_numbers) if blocked_by_numbers else "none")
    )
    return "\n".join(lines)


def _materialize_to_github(
    tasks: list[Task], project_context, store: Store, client: GitHubClient | None,
) -> list[int]:
    if not project_context.repository:
        return []
    client = client or GitHubClient(store)
    task_to_issue: dict[str, int] = {}
    created: list[int] = []

    for task in _topological_order(tasks):
        if task.state == TaskState.NEEDS_LUCAS:
            continue
        blocked_by_numbers = [
            task_to_issue[dep_id] for dep_id in task.dependencies if dep_id in task_to_issue
        ]
        result = client.create_issue(
            project_context.repository, task.title, _issue_body(task, blocked_by_numbers),
            ["origin:planner"], task.correlation_id,
        )
        if result.get("available") and isinstance(result.get("number"), int):
            task_to_issue[task.id] = result["number"]
            created.append(result["number"])

    return created


def _format_fallback_block(tasks: list[Task]) -> str:
    by_id = {task.id: task for task in tasks}
    lines = ["## Novas tarefas do planner", ""]
    for task in tasks:
        if task.state == TaskState.NEEDS_LUCAS:
            continue
        dep_titles = [by_id[dep_id].title for dep_id in task.dependencies if dep_id in by_id]
        lines.append(f"- [ ] {task.title} (depende de: {', '.join(dep_titles) if dep_titles else 'nenhuma'})")
        if task.objective and task.objective != task.title:
            lines.append(f"  {task.objective}")
    return "\n".join(lines) + "\n"


def materialize_plan(
    plan_result, project_context, store: Store, *, client: GitHubClient | None = None,
) -> list[int]:
    """Turns plan_result.tasks into the project's REAL task queue.

    Returns the GitHub Issue numbers created (in topological/creation
    order) when `project_context.task_source` indicates GitHub Issues is
    this project's queue (section 13). Each Issue's body records its
    BLOCKED_BY as real Issue numbers - only the tasks created earlier in
    this same batch can be referenced that way, so tasks are created in
    dependency order (see _topological_order). Idempotency for a replay
    of the same plan comes from passing each Task's own stable
    correlation_id through to #17's GitHubClient.create_issue, which
    already dedupes on (repo, correlation_id) - this module adds none of
    its own.

    When the project does NOT use GitHub Issues, this never forces that
    convention on it (section 13): it appends a generic formatted text
    block to the project's own queue file, resolved from task_source, and
    always returns `[]` (there is nothing to report as an Issue number).
    A project whose queue file can't be resolved from task_source simply
    gets no fallback write - migrating it onto a new format is out of
    scope. A task flagged NEEDS_LUCAS by the planner is never
    materialized either way; it still needs a human decision first.
    """
    tasks = list(plan_result.tasks)
    if not tasks:
        return []

    if _uses_github_issues(project_context):
        return _materialize_to_github(tasks, project_context, store, client)

    path = _resolve_fallback_path(project_context)
    if path is not None:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n" + _format_fallback_block(_topological_order(tasks)))
        except OSError:
            pass
    return []
