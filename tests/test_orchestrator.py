"""Tests for orchestrator.orchestrator (issue #30): the daily-cycle wiring
that composes the Scheduler (#25), queue promotion (#28), materialize_plan
(#23) and Paperclip (#18) into one pass, plus run_cutoff and run_report.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.events import EventType, emit
from orchestrator.github_client import GitHubClient
from orchestrator.models import AgentClass, ExecutionMode, Task, TaskState
from orchestrator.orchestrator import run_cutoff, run_daily_cycle, run_report
from orchestrator.persistence import Store
from orchestrator.project_resolver import ProjectContext

PROJECT = ProjectContext(
    canonical_id="hub", root="/repo", repository="owner/repo",
    task_source="GitHub Issues (`gh issue list` in this repo) - the real work queue",
)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class FakeGitHub:
    def __init__(self):
        self.issues, self.posts = [], []

    def run(self, args, **kwargs):
        method = args[args.index('--method') + 1]
        if method == 'GET':
            return subprocess.CompletedProcess(args, 0, json.dumps([self.issues]), '')
        if method == 'PATCH':
            number = int(args[args.index('--method') + 2].rsplit('/', 1)[-1])
            body = json.loads(kwargs['input'])
            for issue in self.issues:
                if issue['number'] == number:
                    issue['body'] = body['body']
                    return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')
            return subprocess.CompletedProcess(args, 1, '', 'not found')
        body = json.loads(kwargs['input'])
        self.posts.append(body)
        issue = {'number': len(self.posts), **body, 'state': 'open'}
        self.issues.append(issue)
        return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')


class FakePaperclipSession:
    def __init__(self, *, available=True):
        self.available = available
        self.created = []

    def create_task_idempotent(self, company_id, title, description, correlation_id, store=None):
        self.created.append((company_id, title, correlation_id))
        if not self.available:
            return {"available": False, "reason": "connection_refused"}
        return {"available": True, "task_id": f"pc-{len(self.created)}"}


def _task(**overrides):
    defaults = dict(
        title="Task", objective="Do the thing", agent_class=AgentClass.FLEX,
        execution_mode=ExecutionMode.PARALLEL, state=TaskState.PLANNED,
        project_id="hub",
    )
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


def _github_client(store, github):
    return GitHubClient(store, run_fn=github.run, timeout_seconds=5)


def test_daily_cycle_promotes_ready_dependent_and_leaves_blocked(store):
    # 3 tasks (test plan): one already free, one dependent on it, one
    # explicitly BLOCKED - only the first two are ever eligible.
    base = _task(title="Base work", state=TaskState.NEXT_CYCLE)
    store.save_task(base)
    dependent = _task(title="Dependent work", state=TaskState.PLANNED, dependencies=[base.id])
    store.save_task(dependent)
    blocked = _task(title="Blocked work", state=TaskState.BLOCKED)
    store.save_task(blocked)

    clock = Clock(datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)
    paperclip = FakePaperclipSession()

    result = run_daily_cycle(
        store, PROJECT, client=client, paperclip_session=paperclip, company_id="acme",
        clock=clock, paperclip_available=lambda: True, paperclip_snapshot=lambda: {"available": True},
    )

    assert result.cycle_fired is True
    # base: NEXT_CYCLE -> READY via scheduler.on_cycle_start (deps already DONE trivially, none).
    assert base.id in result.ready_task_ids
    # dependent is only PLANNED with an unmet dependency (base isn't DONE) -
    # get_promotable_tasks must NOT promote it yet.
    assert dependent.id not in result.ready_task_ids
    assert blocked.id not in result.ready_task_ids
    assert store.get_task(blocked.id).state == TaskState.BLOCKED

    assert len(result.created_issue_numbers) == 1
    assert github.posts[0]['title'] == "Base work"
    assert paperclip.created == [("acme", "Base work", base.correlation_id)]


def test_daily_cycle_promotes_dependent_once_dependency_is_done(store):
    base = _task(title="Base work", state=TaskState.DONE)
    store.save_task(base)
    dependent = _task(title="Dependent work", state=TaskState.PLANNED, dependencies=[base.id])
    store.save_task(dependent)

    clock = Clock(datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)

    result = run_daily_cycle(store, PROJECT, client=client, clock=clock)

    assert dependent.id in result.promoted_task_ids
    assert store.get_task(dependent.id).state == TaskState.READY


def test_daily_cycle_is_idempotent_on_rerun(store):
    task = _task(title="Solo work", state=TaskState.NEXT_CYCLE)
    store.save_task(task)
    clock = Clock(datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)
    paperclip = FakePaperclipSession()

    first = run_daily_cycle(store, PROJECT, client=client, paperclip_session=paperclip, company_id="acme", clock=clock)
    second = run_daily_cycle(store, PROJECT, client=client, paperclip_session=paperclip, company_id="acme", clock=clock)

    assert first.created_issue_numbers == [1]
    # Re-running the same day: cycle_start's own guard fires only once,
    # and materialize_plan/create_task_idempotent are no-ops for a task
    # already materialized - no duplicate Issue or Paperclip task.
    assert second.cycle_fired is False
    assert len(github.posts) == 1
    assert len(paperclip.created) == 2  # called again, but idempotent server-side per its own contract


def test_run_cutoff_does_not_touch_in_progress_tasks(store):
    in_progress = _task(title="Running", state=TaskState.IN_PROGRESS)
    store.save_task(in_progress)
    clock = Clock(datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc))

    fired = run_cutoff(store, clock=clock)

    assert fired is True
    assert store.get_task(in_progress.id).state == TaskState.IN_PROGRESS


def test_run_report_collects_raw_state_and_deltas(store):
    now = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=9)
    clock = Clock(now)

    in_progress = _task(title="Running", state=TaskState.IN_PROGRESS)
    store.save_task(in_progress)
    blocked = _task(title="Stuck", state=TaskState.BLOCKED)
    store.save_task(blocked)
    needs_lucas = _task(title="Ask Lucas", state=TaskState.NEEDS_LUCAS)
    store.save_task(needs_lucas)
    next_cycle = _task(title="Tomorrow", state=TaskState.NEXT_CYCLE)
    store.save_task(next_cycle)

    emit(store, EventType.TASK_COMPLETED, {"task_id": "x"}, created_at=since + timedelta(hours=1))
    emit(store, EventType.MERGE_COMPLETED, {"pr": 1}, created_at=since + timedelta(hours=2))
    emit(store, EventType.BUG_FOUND, {}, created_at=since + timedelta(hours=3))

    report = run_report(store, since=since, clock=clock)

    assert report["tasks_completed"] == 1
    assert report["merges_completed"] == 1
    assert report["bugs_found"] == 1
    assert report["tasks_in_progress"] == [in_progress.id]
    assert report["tasks_currently_blocked"] == [blocked.id]
    assert report["needs_lucas"] == [needs_lucas.id]
    assert report["next_cycle"] == [next_cycle.id]
    assert report["since"] == since
    assert report["generated_at"] == now


def test_run_report_defaults_since_to_last_report_call(store):
    first_now = datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)
    second_now = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)
    run_report(store, clock=Clock(first_now))

    emit(store, EventType.TASK_COMPLETED, {"task_id": "y"}, created_at=first_now + timedelta(hours=1))

    report = run_report(store, clock=Clock(second_now))

    assert report["since"] == first_now
    assert report["tasks_completed"] == 1
