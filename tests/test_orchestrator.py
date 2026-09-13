"""Tests for orchestrator.orchestrator (issue #30): the daily-cycle wiring
that composes the Scheduler (#25), queue promotion (#28), priority/
availability (#26/#31), materialize_plan (#23) and Paperclip (#18) into one
pass, plus run_cutoff and run_report/mark_report_delivered.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.agent_availability import mark_rate_limited
from orchestrator.events import EventType, emit
from orchestrator.github_client import GitHubClient
from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task, TaskState
from orchestrator.orchestrator import mark_report_delivered, run_cutoff, run_daily_cycle, run_report
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
    """`confirm_assignment=False` simulates Paperclip accepting the create
    call but never actually reflecting an assignee (Review Task #131
    finding #4's fake)."""

    def __init__(self, *, available=True, confirm_assignment=True):
        self.available = available
        self.confirm_assignment = confirm_assignment
        self.created = []  # (company_id, title, correlation_id, assignee_agent_id)
        self._tasks = {}

    def create_task_idempotent(self, company_id, title, description, correlation_id,
                               assignee_agent_id=None, *, store=None):
        self.created.append((company_id, title, correlation_id, assignee_agent_id))
        if not self.available:
            return {"available": False, "reason": "connection_refused"}
        task_id = f"pc-{len(self.created)}"
        remote = {
            "id": task_id, "title": title,
            "assigneeAgentId": assignee_agent_id if self.confirm_assignment else None,
        }
        self._tasks[task_id] = remote
        return {"available": True, "task_id": task_id, "task": remote}

    def get_task_status(self, company_id, task_id):
        task = self._tasks.get(task_id)
        if task is None:
            return {"available": False, "reason": "not_found"}
        return {"available": True, "task_id": task_id, "status": "open", "task": task}


def _fake_find_agent(name_query):
    return {"id": f"agent-{name_query.lower()}"}, None


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


@pytest.fixture(autouse=True)
def _patch_find_agent(monkeypatch):
    monkeypatch.setattr("orchestrator.orchestrator.paperclip_client.find_agent", _fake_find_agent)


def _github_client(store, github):
    return GitHubClient(store, run_fn=github.run, timeout_seconds=5)


def test_daily_cycle_promotes_ready_dependent_and_leaves_blocked(store):
    # 3 tasks (test plan): one already free, one dependent on it, one
    # explicitly BLOCKED - only the first two are ever eligible.
    base = _task(title="Base work", state=TaskState.NEXT_CYCLE,
                 preferred_agent=AgentName.CLAUDE)
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
    assert paperclip.created == [("acme", "Base work", base.correlation_id, "agent-claude")]
    assert base.id in result.assigned_task_ids
    assert result.dispatch_incomplete_task_ids == []


def test_daily_cycle_promotes_dependent_once_dependency_is_done(store):
    base = _task(title="Base work", state=TaskState.DONE)
    store.save_task(base)
    dependent = _task(title="Dependent work", state=TaskState.PLANNED, dependencies=[base.id])
    store.save_task(dependent)

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))  # inside the admission window
    github = FakeGitHub()
    client = _github_client(store, github)

    result = run_daily_cycle(store, PROJECT, client=client, clock=clock)

    assert dependent.id in result.promoted_task_ids
    assert store.get_task(dependent.id).state == TaskState.READY


def test_daily_cycle_never_promotes_planned_task_before_cycle_start(store):
    # Review Task #131 finding #1's exact repro: a PLANNED task whose
    # dependency is already DONE must NOT be promoted before cycle_start
    # (default 08:00) - only scheduler.admit_task's own window check may
    # decide that, never a direct state write.
    base = _task(title="Base", state=TaskState.DONE)
    store.save_task(base)
    task = _task(title="Too early", state=TaskState.PLANNED, dependencies=[base.id])
    store.save_task(task)

    # Europe/Lisbon is UTC+1 in September (DST) - 05:00 UTC is 06:00
    # local, safely before the default 08:00 cycle_start.
    clock = Clock(datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)

    result = run_daily_cycle(store, PROJECT, client=client, clock=clock)

    # admit_task's own documented contract: before cycle_start, PLANNED
    # becomes NEXT_CYCLE, never READY.
    assert store.get_task(task.id).state == TaskState.NEXT_CYCLE
    assert task.id not in result.promoted_task_ids
    assert task.id not in result.ready_task_ids


def test_daily_cycle_never_promotes_planned_task_after_persisted_cutoff_even_if_clock_moves_back(store):
    # Review Task #131 finding #1's other repro: cutoff already persisted
    # for today, then the clock moves BACK into the window - the
    # PERSISTED guard must still win, not the raw time-of-day check.
    base = _task(title="Base", state=TaskState.DONE)
    store.save_task(base)
    task = _task(title="Late arrival", state=TaskState.PLANNED, dependencies=[base.id])
    store.save_task(task)

    run_cutoff(store, clock=Clock(datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)))

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))  # moved back into the window
    github = FakeGitHub()
    client = _github_client(store, github)

    result = run_daily_cycle(store, PROJECT, client=client, clock=clock)

    assert store.get_task(task.id).state == TaskState.NEXT_CYCLE
    assert task.id not in result.ready_task_ids


def test_daily_cycle_never_dispatches_ready_task_with_unmet_dependency(store):
    # Review Task #131 finding #3: a task inserted directly as READY with
    # an unmet dependency must never be materialized or dispatched.
    # BLOCKED (not just any queued state) so the blocker itself is never
    # independently promotable/materialized either - isolating the check.
    blocker = _task(title="Blocker", state=TaskState.BLOCKED)
    store.save_task(blocker)
    task = _task(title="Should not dispatch", state=TaskState.READY, dependencies=[blocker.id])
    store.save_task(task)

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)
    paperclip = FakePaperclipSession()

    result = run_daily_cycle(store, PROJECT, client=client, paperclip_session=paperclip,
                             company_id="acme", clock=clock)

    assert task.id not in result.ready_task_ids
    assert result.created_issue_numbers == []
    assert paperclip.created == []


def test_daily_cycle_dispatches_urgent_before_low(store):
    # Review Task #131 finding #3: priority order, not creation order.
    low = _task(title="Low priority", state=TaskState.READY, priority="low")
    store.save_task(low)
    urgent = _task(title="Urgent priority", state=TaskState.READY, priority="urgent")
    store.save_task(urgent)

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)

    result = run_daily_cycle(store, PROJECT, client=client, clock=clock)

    assert result.ready_task_ids == [urgent.id, low.id]
    assert [post['title'] for post in github.posts] == ["Urgent priority", "Low priority"]


def test_daily_cycle_never_dispatches_another_projects_task(store):
    # Review Task #131 finding #2: routing must not cross projects.
    other = _task(title="Belongs elsewhere", state=TaskState.READY, project_id="other")
    store.save_task(other)
    own = _task(title="Belongs here", state=TaskState.READY, project_id="hub")
    store.save_task(own)

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)
    paperclip = FakePaperclipSession()

    result = run_daily_cycle(store, PROJECT, client=client, paperclip_session=paperclip,
                             company_id="acme", clock=clock)

    assert own.id in result.ready_task_ids
    assert other.id not in result.ready_task_ids
    assert [post['title'] for post in github.posts] == ["Belongs here"]
    assert [entry[1] for entry in paperclip.created] == ["Belongs here"]


def test_daily_cycle_skips_paperclip_dispatch_when_all_concrete_agents_are_in_cooldown(store):
    # Review Task #131 finding #3/#4: never create executable work with
    # no free concrete agent - both configured candidates are limited.
    task = _task(title="Needs a human agent", state=TaskState.READY,
                 preferred_agent=AgentName.CLAUDE, fallback_agent=AgentName.CODEX,
                 agent_class=AgentClass.CLAUDE)
    store.save_task(task)
    mark_rate_limited(store, AgentName.CLAUDE, "quota")
    mark_rate_limited(store, AgentName.CODEX, "quota")

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)
    paperclip = FakePaperclipSession()

    result = run_daily_cycle(store, PROJECT, client=client, paperclip_session=paperclip,
                             company_id="acme", clock=clock)

    assert task.id in result.ready_task_ids  # still tracked/materialized on GitHub
    assert len(result.created_issue_numbers) == 1
    assert paperclip.created == []  # never dispatched with no free agent
    assert task.id not in result.paperclip_created_task_ids


def test_daily_cycle_reports_dispatch_incomplete_when_assignment_is_not_confirmed(store):
    # Review Task #131 finding #4: a created-but-unassigned Paperclip task
    # must be surfaced as incomplete, never silently counted as progress.
    task = _task(title="Unconfirmed assignment", state=TaskState.READY,
                preferred_agent=AgentName.CLAUDE, agent_class=AgentClass.CLAUDE)
    store.save_task(task)

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))
    github = FakeGitHub()
    client = _github_client(store, github)
    paperclip = FakePaperclipSession(confirm_assignment=False)

    result = run_daily_cycle(store, PROJECT, client=client, paperclip_session=paperclip,
                             company_id="acme", clock=clock)

    assert task.id in result.paperclip_created_task_ids
    assert task.id not in result.assigned_task_ids
    assert task.id in result.dispatch_incomplete_task_ids


def test_daily_cycle_materializes_to_fallback_queue_without_a_github_client(store, tmp_path):
    # Review Task #131 finding #5: materialize_plan must run even with no
    # GitHub client at all when the project's own queue file applies.
    fila_project = ProjectContext(
        canonical_id="pdr", root=str(tmp_path),
        task_source=r"docs\ai\FILA.md in this repo - a live, dependency-ordered work queue. Not GitHub Issues for this project.",
    )
    fila_path = tmp_path / "docs" / "ai" / "FILA.md"
    fila_path.parent.mkdir(parents=True)
    fila_path.write_text("# Fila\n", encoding="utf-8")
    task = _task(title="Fallback task", state=TaskState.READY, project_id="pdr")
    store.save_task(task)

    clock = Clock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))

    result = run_daily_cycle(store, fila_project, client=None, clock=clock)

    assert fila_path.exists()
    assert "Fallback task" in fila_path.read_text(encoding="utf-8")
    assert result.created_issue_numbers == []


def test_daily_cycle_is_idempotent_on_rerun(store):
    task = _task(title="Solo work", state=TaskState.NEXT_CYCLE, preferred_agent=AgentName.CLAUDE)
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


def test_run_report_is_a_pure_read_and_never_consumes_the_window(store):
    # Review Task #131 finding #6: collecting the report must not itself
    # advance the cursor - only mark_report_delivered does, once delivery
    # is confirmed. Two collections of the SAME window (e.g. the first
    # send failed downstream) must report the SAME counts.
    now = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)
    emit(store, EventType.TASK_COMPLETED, {"task_id": "x"},
         created_at=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc))

    first = run_report(store, since=datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc), clock=Clock(now))
    second = run_report(store, since=datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc), clock=Clock(now))

    assert first["tasks_completed"] == 1
    assert second["tasks_completed"] == 1  # NOT silently reset to 0 by the first call


def test_run_report_defaults_since_to_last_delivered_report(store):
    first_now = datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)
    second_now = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)

    run_report(store, clock=Clock(first_now))
    mark_report_delivered(store, clock=Clock(first_now))

    emit(store, EventType.TASK_COMPLETED, {"task_id": "y"}, created_at=first_now + timedelta(hours=1))

    report = run_report(store, clock=Clock(second_now))

    assert report["since"] == first_now
    assert report["tasks_completed"] == 1
