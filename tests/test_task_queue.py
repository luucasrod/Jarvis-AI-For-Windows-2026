"""Independent progress and dependency reconsideration scenarios (#28)."""
import pytest

from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store
from orchestrator.task_queue import get_promotable_tasks


def make_task(name, state=TaskState.READY, dependencies=None):
    return Task(title=name, objective=name, state=state, dependencies=dependencies or [])


@pytest.mark.parametrize("blocked_state", [TaskState.BLOCKED, TaskState.NEEDS_LUCAS])
def test_blocked_a_does_not_stop_independent_b_and_c(blocked_state):
    a = make_task("A", blocked_state)
    b, c = make_task("B"), make_task("C", TaskState.NEXT_CYCLE)
    d = make_task("D", dependencies=[a.id])
    e = make_task("E", dependencies=[d.id])
    tasks = [a, b, c, d, e]
    before = [task.to_dict() for task in tasks]
    assert get_promotable_tasks(tasks) == [b, c]
    assert [task.to_dict() for task in tasks] == before

    # Resolving the blocker lets A run, not its still-dependent descendants.
    a.state = TaskState.READY
    assert get_promotable_tasks(tasks) == [a, b, c]
    a.state = TaskState.DONE
    assert get_promotable_tasks(tasks) == [b, c, d]
    d.state = TaskState.DONE
    assert get_promotable_tasks(tasks) == [b, c, e]


@pytest.mark.parametrize("state", list(TaskState))
def test_only_queued_states_are_promotable(state):
    item = make_task("Task", state)
    expected = [item] if state in (TaskState.PLANNED, TaskState.NEXT_CYCLE, TaskState.READY) else []
    assert get_promotable_tasks([item]) == expected


def test_all_dependencies_must_be_done():
    a = make_task("A", TaskState.DONE)
    b = make_task("B", TaskState.IN_REVIEW)
    c = make_task("C", dependencies=[a.id, b.id])
    assert get_promotable_tasks([a, b, c]) == []
    b.state = TaskState.DONE
    assert get_promotable_tasks([a, b, c]) == [c]


def test_missing_self_and_cyclic_dependencies_do_not_block_independent_work():
    missing = make_task("Missing", dependencies=["unknown"])
    self_ref = make_task("Self")
    self_ref.dependencies = [self_ref.id]
    a, b = make_task("Cycle A"), make_task("Cycle B")
    a.dependencies, b.dependencies = [b.id], [a.id]
    independent = make_task("Independent")
    assert get_promotable_tasks([missing, self_ref, a, b, independent]) == [independent]


def test_duplicate_dependency_is_harmless_but_duplicate_id_is_rejected():
    done = make_task("Done", TaskState.DONE)
    item = make_task("Task", dependencies=[done.id, done.id])
    assert get_promotable_tasks([done, item]) == [item]
    conflicting = make_task("Conflict", TaskState.BLOCKED)
    conflicting.id = done.id
    with pytest.raises(ValueError, match="Duplicate task id"):
        get_promotable_tasks([done, conflicting, item])


def test_empty_snapshot_and_input_order_without_artificial_cap():
    assert get_promotable_tasks([]) == []
    tasks = [make_task(str(index)) for index in range(200)]
    assert get_promotable_tasks(tasks) == tasks


def test_persisted_resolution_is_reconsidered_after_restart(tmp_path):
    path = tmp_path / "state.db"
    a = make_task("A", TaskState.NEEDS_LUCAS)
    d = make_task("D", TaskState.NEXT_CYCLE, [a.id])
    independent = make_task("Independent")
    store = Store(path)
    try:
        for item in [a, d, independent]:
            store.save_task(item)
        assert [item.id for item in get_promotable_tasks(store.list_tasks())] == [independent.id]
        a.state = TaskState.DONE
        store.save_task(a)
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert {item.id for item in get_promotable_tasks(reopened.list_tasks())} == {d.id, independent.id}
        assert reopened.get_task(d.id).state == TaskState.NEXT_CYCLE
    finally:
        reopened.close()
