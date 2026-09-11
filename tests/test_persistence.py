"""Tests for orchestrator.persistence (issue #13)."""
from orchestrator.models import AgentClass, Task, TaskState
from orchestrator.persistence import Store


def test_save_and_get_task(tmp_path):
    store = Store(tmp_path / "state.db")
    task = Task(title="Fazer X", objective="Objetivo Y", state=TaskState.READY)

    store.save_task(task)
    fetched = store.get_task(task.id)

    assert fetched is not None
    assert fetched.title == "Fazer X"
    assert fetched.state == TaskState.READY
    store.close()


def test_get_task_missing_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    assert store.get_task("does-not-exist") is None
    store.close()


def test_list_tasks_filters_by_state(tmp_path):
    store = Store(tmp_path / "state.db")
    ready = Task(title="A", objective="obj", state=TaskState.READY)
    blocked = Task(title="B", objective="obj", state=TaskState.BLOCKED)
    store.save_task(ready)
    store.save_task(blocked)

    all_tasks = store.list_tasks()
    ready_tasks = store.list_tasks(state=TaskState.READY)

    assert len(all_tasks) == 2
    assert len(ready_tasks) == 1
    assert ready_tasks[0].title == "A"
    store.close()


def test_save_task_upsert_updates_existing(tmp_path):
    store = Store(tmp_path / "state.db")
    task = Task(title="A", objective="obj", state=TaskState.INBOX)
    store.save_task(task)

    task.state = TaskState.DONE
    store.save_task(task)

    fetched = store.get_task(task.id)
    assert fetched.state == TaskState.DONE
    assert len(store.list_tasks()) == 1
    store.close()


def test_restart_recovers_state(tmp_path):
    db_path = tmp_path / "state.db"
    task = Task(title="Sobrevive restart", objective="obj", state=TaskState.IN_PROGRESS)

    store1 = Store(db_path)
    store1.save_task(task)
    store1.close()

    store2 = Store(db_path)
    fetched = store2.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "Sobrevive restart"
    assert fetched.state == TaskState.IN_PROGRESS
    store2.close()


def test_decisions_pending_and_resolve(tmp_path):
    store = Store(tmp_path / "state.db")
    store.save_decision("corr-1", "Precisa de credencial X", task_id="task-1")

    pending = store.get_pending_decisions()
    assert len(pending) == 1
    assert pending[0]["correlation_id"] == "corr-1"

    store.resolve_decision("corr-1", response="opcao B")
    assert store.get_pending_decisions() == []
    store.close()


def test_rate_limit_set_get_clear(tmp_path):
    store = Store(tmp_path / "state.db")
    assert store.get_rate_limit("codex") is None

    store.set_rate_limit("codex", "usage limit", reset_at="2026-01-01T00:00:00+00:00")
    limit = store.get_rate_limit("codex")
    assert limit["reason"] == "usage limit"
    assert limit["reset_at"] == "2026-01-01T00:00:00+00:00"

    store.clear_rate_limit("codex")
    assert store.get_rate_limit("codex") is None
    store.close()


def test_sync_state_roundtrip(tmp_path):
    store = Store(tmp_path / "state.db")
    assert store.get_sync_value("last_github_sync") is None

    store.set_sync_value("last_github_sync", "2026-09-11T12:00:00Z")
    assert store.get_sync_value("last_github_sync") == "2026-09-11T12:00:00Z"
    store.close()


def test_idempotency_key_prevents_duplicate_check(tmp_path):
    store = Store(tmp_path / "state.db")
    assert store.has_idempotency_key("corr-1", "github_issue") is False

    store.record_idempotency_key("corr-1", "github_issue")
    assert store.has_idempotency_key("corr-1", "github_issue") is True

    # recording twice must not raise or duplicate
    store.record_idempotency_key("corr-1", "github_issue")
    assert store.has_idempotency_key("corr-1", "github_issue") is True
    store.close()
