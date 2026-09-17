"""Tests for orchestrator.paperclip_sync (issue #157)."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator.paperclip_sync as paperclip_sync
from orchestrator.config import OrchestratorConfig
from orchestrator.github_client import GitHubClient
from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task, TaskState
from orchestrator.paperclip_sync import process_paperclip_sync_tick, sync_dispatched_tasks
from orchestrator.persistence import Store
from orchestrator.project_resolver import ProjectContext, ResolveError

PROJECT = ProjectContext(
    canonical_id="hub", root="/repo", repository="owner/repo",
    task_source="GitHub Issues (`gh issue list` in this repo) - the real work queue",
)


class FakeGitHub:
    def __init__(self):
        self.issues, self.posts = [], []

    def run(self, args, **kwargs):
        method = args[args.index('--method') + 1]
        if method == 'GET':
            return subprocess.CompletedProcess(args, 0, json.dumps([self.issues]), '')
        body = json.loads(kwargs['input'])
        self.posts.append(body)
        issue = {'number': len(self.posts), **body, 'state': 'open'}
        self.issues.append(issue)
        return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')


class FakePaperclipSession:
    def __init__(self, *, config=None):
        self.config = config or OrchestratorConfig()
        self.created = []

    def create_task_idempotent(self, company_id, title, description, correlation_id,
                               assignee_agent_id=None, *, store=None):
        self.created.append((company_id, title, correlation_id, assignee_agent_id))
        task_id = f"pc-{len(self.created)}"
        return {"available": True, "task_id": task_id, "task": {"id": task_id, "title": title}}

    def get_task_status(self, company_id, task_id):
        return {"available": True, "task_id": task_id, "status": "open"}


def _fake_find_agent(name_query, *, company_id=None, base_url=None, timeout=None):
    return {"id": f"agent-{name_query.lower()}", "_company_id": company_id}, None


def _task(**overrides):
    defaults = dict(
        title="Task", objective="Do the thing", agent_class=AgentClass.FLEX,
        execution_mode=ExecutionMode.PARALLEL, state=TaskState.READY, project_id="hub",
    )
    defaults.update(overrides)
    return Task(**defaults)


class _FakeResolver:
    def __init__(self, project=None, *, error=None):
        self._project = project
        self._error = error

    def resolve(self, query):
        if self._error is not None:
            return self._error
        return self._project or PROJECT


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _patch_find_agent(monkeypatch):
    monkeypatch.setattr("orchestrator.orchestrator.paperclip_client.find_agent", _fake_find_agent)


# --- sync_dispatched_tasks ------------------------------------------------------

def test_no_tracked_tasks_is_a_noop(store):
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result == {"synced_task_ids": [], "dispatch_results": {}}


def test_planned_task_without_a_dispatch_is_never_touched(store):
    task = _task(state=TaskState.PLANNED)
    store.save_task(task)
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == []
    assert store.get_task(task.id).state == TaskState.PLANNED


def test_task_without_project_id_is_skipped(store):
    task = _task(project_id=None)
    store.save_task(task)
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == []


def test_unresolvable_project_is_skipped(store):
    task = _task()
    store.save_task(task)
    result = sync_dispatched_tasks(store, resolver=_FakeResolver(error=ResolveError(reason="nao encontrado")))
    assert result["synced_task_ids"] == []
    assert store.get_task(task.id).state == TaskState.READY


def test_no_matching_company_is_skipped(store, monkeypatch):
    task = _task()
    store.save_task(task)
    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: None)
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == []


def test_not_yet_dispatched_task_is_skipped_without_a_status_call(store, monkeypatch):
    task = _task()
    store.save_task(task)
    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: "company-1")
    monkeypatch.setattr(paperclip_sync, "find_created_task_id", lambda *a, **k: None)
    status_calls = []
    monkeypatch.setattr(paperclip_sync, "get_task_status",
                        lambda *a, **k: status_calls.append(1) or {"available": True, "status": "done"})
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == []
    assert status_calls == []


def test_dispatched_but_not_done_task_stays_ready(store, monkeypatch):
    task = _task()
    store.save_task(task)
    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: "company-1")
    monkeypatch.setattr(paperclip_sync, "find_created_task_id", lambda *a, **k: "pc-1")
    monkeypatch.setattr(paperclip_sync, "get_task_status",
                        lambda *a, **k: {"available": True, "status": "in_progress"})
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == []
    assert store.get_task(task.id).state == TaskState.READY


def test_unavailable_status_is_skipped_without_crashing(store, monkeypatch):
    task = _task()
    store.save_task(task)
    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: "company-1")
    monkeypatch.setattr(paperclip_sync, "find_created_task_id", lambda *a, **k: "pc-1")
    monkeypatch.setattr(paperclip_sync, "get_task_status",
                        lambda *a, **k: {"available": False, "reason": "offline"})
    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == []
    assert store.get_task(task.id).state == TaskState.READY


def test_done_task_is_marked_done_and_its_project_is_redispatched(store, monkeypatch):
    # The end-to-end happy path (issue #157's whole point): a task
    # Paperclip reports done gets marked DONE locally, and the project
    # that owns it goes through the SAME real run_daily_cycle dispatch
    # path #152 uses for a fresh confirmation - so a dependent task that
    # was only blocked on this one now materializes/dispatches for real.
    done = _task(title="Done task", state=TaskState.READY)
    dependent = _task(title="Dependent task", state=TaskState.PLANNED, dependencies=[done.id])
    store.save_task(done)
    store.save_task(dependent)

    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: "company-1")
    monkeypatch.setattr(paperclip_sync, "find_created_task_id",
                        lambda company_id, correlation_id, **k: "pc-1" if correlation_id == done.correlation_id else None)
    monkeypatch.setattr(paperclip_sync, "get_task_status", lambda *a, **k: {"available": True, "status": "done"})

    github = FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    result = sync_dispatched_tasks(
        store, resolver=_FakeResolver(), client=client, paperclip_session_factory=FakePaperclipSession,
    )

    assert result["synced_task_ids"] == [done.id]
    assert store.get_task(done.id).state == TaskState.DONE
    assert "hub" in result["dispatch_results"]
    # The dependent task's blocker is now DONE - it must have been
    # admitted and dispatched too, in the SAME call.
    assert len(github.posts) == 1
    assert github.posts[0]["title"] == "Dependent task"


def test_multiple_done_tasks_in_the_same_project_only_redispatch_once(store, monkeypatch):
    first = _task(title="First", state=TaskState.READY)
    second = _task(title="Second", state=TaskState.READY)
    store.save_task(first)
    store.save_task(second)

    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: "company-1")
    monkeypatch.setattr(paperclip_sync, "find_created_task_id", lambda *a, **k: "pc-x")
    monkeypatch.setattr(paperclip_sync, "get_task_status", lambda *a, **k: {"available": True, "status": "done"})

    calls = []
    def fake_run_daily_cycle(*a, **k):
        calls.append(1)
        from orchestrator.orchestrator import DailyCycleResult
        return DailyCycleResult(cycle_fired=False)
    monkeypatch.setattr(paperclip_sync, "run_daily_cycle", fake_run_daily_cycle)

    result = sync_dispatched_tasks(store, resolver=_FakeResolver(), paperclip_session_factory=FakePaperclipSession)
    assert sorted(result["synced_task_ids"]) == sorted([first.id, second.id])
    assert len(calls) == 1


def test_impossible_admission_window_still_marks_done_but_skips_redispatch(store, monkeypatch):
    task = _task()
    store.save_task(task)
    monkeypatch.setattr(paperclip_sync, "resolve_company_id", lambda project, config: "company-1")
    monkeypatch.setattr(paperclip_sync, "find_created_task_id", lambda *a, **k: "pc-1")
    monkeypatch.setattr(paperclip_sync, "get_task_status", lambda *a, **k: {"available": True, "status": "done"})
    monkeypatch.setattr(paperclip_sync, "admission_window_clock", lambda config: None)

    result = sync_dispatched_tasks(store, resolver=_FakeResolver())
    assert result["synced_task_ids"] == [task.id]
    assert store.get_task(task.id).state == TaskState.DONE
    assert result["dispatch_results"] == {}


# --- process_paperclip_sync_tick (throttling) ------------------------------------

def test_first_tick_always_runs(store, monkeypatch):
    calls = []
    monkeypatch.setattr(paperclip_sync, "sync_dispatched_tasks", lambda *a, **k: calls.append(1) or {})
    result = process_paperclip_sync_tick(store)
    assert result == {}
    assert len(calls) == 1


def test_second_tick_within_the_interval_is_skipped(store, monkeypatch):
    calls = []
    monkeypatch.setattr(paperclip_sync, "sync_dispatched_tasks", lambda *a, **k: calls.append(1) or {})
    cfg = OrchestratorConfig(paperclip_sync_interval_seconds=60.0)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    process_paperclip_sync_tick(store, config=cfg, clock=lambda: now)
    result = process_paperclip_sync_tick(store, config=cfg, clock=lambda: now + timedelta(seconds=5))
    assert result is None
    assert len(calls) == 1


def test_tick_runs_again_once_the_interval_has_elapsed(store, monkeypatch):
    calls = []
    monkeypatch.setattr(paperclip_sync, "sync_dispatched_tasks", lambda *a, **k: calls.append(1) or {})
    cfg = OrchestratorConfig(paperclip_sync_interval_seconds=60.0)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    process_paperclip_sync_tick(store, config=cfg, clock=lambda: now)
    result = process_paperclip_sync_tick(store, config=cfg, clock=lambda: now + timedelta(seconds=61))
    assert result == {}
    assert len(calls) == 2


def test_malformed_stored_timestamp_is_treated_as_never_ran(store, monkeypatch):
    store.set_sync_value(paperclip_sync._LAST_SYNC_KEY, "not-a-timestamp")
    calls = []
    monkeypatch.setattr(paperclip_sync, "sync_dispatched_tasks", lambda *a, **k: calls.append(1) or {})
    result = process_paperclip_sync_tick(store)
    assert result == {}
    assert len(calls) == 1
