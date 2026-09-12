"""Dependency-local queue eligibility (#28), without global blocking.

Call get_promotable_tasks with a fresh Store.list_tasks() snapshot whenever a
task completes or a blocker is resolved. This function never changes states:
a dependency wait only excludes a task from the returned list, so it can become
eligible on the next snapshot without clearing a planner/human BLOCKED state.
The scheduler/runtime still applies cycle, cutoff and execution-agent policy.
"""
from __future__ import annotations

from orchestrator.models import Task, TaskState

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
