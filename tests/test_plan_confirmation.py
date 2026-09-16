"""Tests for orchestrator.plan_confirmation (issue #152)."""
import json
import subprocess

import pytest

import orchestrator.plan_confirmation as plan_confirmation
from orchestrator.config import OrchestratorConfig
from orchestrator.github_client import GitHubClient
from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task, TaskState
from orchestrator.paperclip_ops import PaperclipSession
from orchestrator.persistence import Store
from orchestrator.plan_confirmation import _resolve_company_id, execute_confirmed_plan
from orchestrator.project_resolver import ProjectContext

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
        self._tasks = {}

    def create_task_idempotent(self, company_id, title, description, correlation_id,
                               assignee_agent_id=None, *, store=None):
        self.created.append((company_id, title, correlation_id, assignee_agent_id))
        task_id = f"pc-{len(self.created)}"
        remote = {"id": task_id, "title": title, "assigneeAgentId": assignee_agent_id}
        self._tasks[task_id] = remote
        return {"available": True, "task_id": task_id, "task": remote}

    def get_task_status(self, company_id, task_id):
        task = self._tasks.get(task_id)
        if task is None:
            return {"available": False, "reason": "not_found"}
        return {"available": True, "task_id": task_id, "status": "open", "task": task}


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
    def __init__(self, project=None):
        self._project = project or PROJECT

    def resolve(self, query):
        return self._project


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _patch_find_agent(monkeypatch):
    monkeypatch.setattr("orchestrator.orchestrator.paperclip_client.find_agent", _fake_find_agent)


# --- _resolve_company_id ------------------------------------------------------

def test_resolve_company_id_exact_match(monkeypatch):
    monkeypatch.setattr(
        plan_confirmation.paperclip_client, "list_companies",
        lambda: ([{"id": "acme-id", "name": "Hub"}], None),
    )
    assert _resolve_company_id(PROJECT, OrchestratorConfig()) == "acme-id"


def test_resolve_company_id_never_matches_near_homonym(monkeypatch):
    # "hub" must never match "Hub-Extra" - same reasoning as #149's
    # voice_facade (Argos vs Argos-Hub).
    monkeypatch.setattr(
        plan_confirmation.paperclip_client, "list_companies",
        lambda: ([{"id": "wrong-id", "name": "Hub-Extra"}], None),
    )
    assert _resolve_company_id(PROJECT, OrchestratorConfig()) is None


def test_resolve_company_id_returns_none_on_paperclip_error(monkeypatch):
    monkeypatch.setattr(plan_confirmation.paperclip_client, "list_companies", lambda: ([], "offline"))
    assert _resolve_company_id(PROJECT, OrchestratorConfig()) is None


# --- execute_confirmed_plan ----------------------------------------------------

def test_full_flow_creates_real_issue_and_assigns_real_agent(store, monkeypatch):
    monkeypatch.setattr(
        plan_confirmation.paperclip_client, "list_companies",
        lambda: ([{"id": "acme-id", "name": "Hub"}], None),
    )
    task = _task(state=TaskState.PLANNED, preferred_agent=AgentName.CLAUDE)
    store.save_task(task)

    github = FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    paperclip = FakePaperclipSession()

    message = execute_confirmed_plan(
        {"objective": "criar tela", "project_id": "hub", "task_ids": [task.id]},
        store=store, client=client, paperclip_session=paperclip, resolver=_FakeResolver(),
    )

    assert len(github.posts) == 1
    assert len(paperclip.created) == 1
    assert "Issues criadas" in message
    assert "atribuida" in message.lower()


def test_no_tasks_never_touches_github_or_paperclip(store):
    message = execute_confirmed_plan(
        {"objective": "x", "project_id": "hub", "task_ids": []}, store=store,
        client=None, paperclip_session=None, resolver=_FakeResolver(),
    )
    assert message == plan_confirmation._NO_TASKS


def test_unresolvable_project_never_touches_github_or_paperclip(store, monkeypatch):
    from orchestrator.project_resolver import ResolveError

    class _FailingResolver:
        def resolve(self, query):
            return ResolveError(reason="nao encontrado")

    task = _task()
    store.save_task(task)

    def fail_client(*a, **k):
        pytest.fail("must not construct a real GitHubClient for an unresolvable project")

    monkeypatch.setattr(plan_confirmation, "GitHubClient", fail_client)

    message = execute_confirmed_plan(
        {"objective": "x", "project_id": "hub", "task_ids": [task.id]},
        store=store, resolver=_FailingResolver(),
    )
    assert "hub" in message
    assert "localmente" in message.lower()


def test_impossible_window_never_touches_github_or_paperclip(store, monkeypatch):
    cfg = OrchestratorConfig(timezone="UTC", cycle_start_time="14:00", cutoff_time="08:00")
    task = _task()
    store.save_task(task)

    def fail_client(*a, **k):
        pytest.fail("must not construct a real GitHubClient when there is no valid dispatch window")

    monkeypatch.setattr(plan_confirmation, "GitHubClient", fail_client)

    message = execute_confirmed_plan(
        {"objective": "x", "project_id": "hub", "task_ids": [task.id]},
        store=store, config=cfg, resolver=_FakeResolver(),
    )
    assert "CYCLE_START_TIME" in message


def test_no_matching_paperclip_company_still_publishes_to_github(store, monkeypatch):
    monkeypatch.setattr(plan_confirmation.paperclip_client, "list_companies", lambda: ([], None))
    task = _task(state=TaskState.PLANNED)
    store.save_task(task)

    github = FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    message = execute_confirmed_plan(
        {"objective": "criar tela", "project_id": "hub", "task_ids": [task.id]},
        store=store, client=client, resolver=_FakeResolver(),
    )

    assert len(github.posts) == 1  # published anyway
    assert "nenhum agente foi acionado" in message.lower()


def test_admission_window_clock_is_inside_the_configured_window():
    cfg = OrchestratorConfig(timezone="UTC", cycle_start_time="08:00", cutoff_time="14:00")
    clock = plan_confirmation._admission_window_clock(cfg)
    instant = clock()
    from datetime import time
    assert time(8, 0) <= instant.time() < time(14, 0)
    # Calling the SAME clock twice must return the identical instant
    # (run_daily_cycle calls it more than once per pass).
    assert clock() == instant


def test_admission_window_clock_stays_inside_a_narrow_window():
    # Independent-review finding: a naive "+30 minutes" could overshoot a
    # narrow/custom window entirely, silently dispatching nothing while
    # claiming success. A 15-minute window's midpoint must still land
    # strictly before cutoff.
    from datetime import time
    cfg = OrchestratorConfig(timezone="UTC", cycle_start_time="08:00", cutoff_time="08:15")
    clock = plan_confirmation._admission_window_clock(cfg)
    instant = clock()
    assert time(8, 0) <= instant.time() < time(8, 15)


def test_admission_window_clock_returns_none_for_an_impossible_window():
    cfg = OrchestratorConfig(timezone="UTC", cycle_start_time="14:00", cutoff_time="08:00")
    assert plan_confirmation._admission_window_clock(cfg) is None
