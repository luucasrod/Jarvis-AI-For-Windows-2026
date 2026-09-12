"""Tests for orchestrator.task_queue.materialize_plan (issue #23)."""
from datetime import datetime, timezone
import json
import subprocess

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.github_client import GitHubClient
from orchestrator.models import AgentClass, ExecutionMode, Task, TaskState
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult
from orchestrator.project_resolver import ProjectContext
from orchestrator.task_queue import _uses_github_issues, materialize_plan


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class GitHub:
    """Fake `gh` subprocess - same shape as test_github_client.py's fake,
    duplicated locally so this suite has no cross-file test dependency."""

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


@pytest.fixture
def setup(tmp_path):
    store, github = Store(tmp_path / 'state.db'), GitHub()
    client = GitHubClient(store, config=OrchestratorConfig(retry_interval_seconds=10),
                          run_fn=github.run, clock=Clock(), timeout_seconds=5)
    yield store, github, client
    store.close()


def _task(**overrides) -> Task:
    defaults = dict(
        title="Task", objective="Do the thing", agent_class=AgentClass.FLEX,
        execution_mode=ExecutionMode.PARALLEL, state=TaskState.PLANNED,
    )
    defaults.update(overrides)
    return Task(**defaults)


GITHUB_PROJECT = ProjectContext(
    canonical_id="hub", root="/repo", repository="owner/repo",
    task_source="GitHub Issues (`gh issue list` in this repo) - the real work queue",
)

FILA_PROJECT = ProjectContext(
    canonical_id="pdr", root=None,
    task_source=r"docs\ai\FILA.md in this repo - a live, dependency-ordered work queue. Not GitHub Issues for this project.",
)


def test_three_dependent_tasks_created_in_topological_order_with_references(setup):
    store, github, client = setup
    base = _task(title="Base work")
    middle = _task(title="Middle work", dependencies=[base.id])
    leaf = _task(title="Leaf work", dependencies=[middle.id])
    # Deliberately out of dependency order to prove materialize_plan reorders.
    plan_result = PlanResult(tasks=[leaf, middle, base])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == [1, 2, 3]
    assert [post['title'] for post in github.posts] == ["Base work", "Middle work", "Leaf work"]
    assert "BLOCKED_BY: none" in github.posts[0]['body']
    assert "BLOCKED_BY: #1" in github.posts[1]['body']
    assert "BLOCKED_BY: #2" in github.posts[2]['body']


def test_replaying_same_plan_does_not_duplicate(setup):
    store, github, client = setup
    task = _task(title="Solo task")
    plan_result = PlanResult(tasks=[task])

    first = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)
    second = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert first == second == [1]
    assert len(github.posts) == 1


def test_needs_lucas_task_is_never_materialized(setup):
    store, github, client = setup
    task = _task(title="Needs a human", state=TaskState.NEEDS_LUCAS)
    plan_result = PlanResult(tasks=[task])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == []
    assert github.posts == []


def test_project_without_repository_configured_returns_empty_without_crashing(setup):
    store, github, client = setup
    project = ProjectContext(canonical_id="hub", repository=None,
                              task_source="GitHub Issues here")
    plan_result = PlanResult(tasks=[_task()])

    assert materialize_plan(plan_result, project, store, client=client) == []
    assert github.posts == []


def test_non_github_project_falls_back_to_queue_file_without_crashing(setup, tmp_path):
    store, github, client = setup
    (tmp_path / "docs" / "ai").mkdir(parents=True)
    fila = tmp_path / "docs" / "ai" / "FILA.md"
    fila.write_text("# Fila existente\n", encoding="utf-8")
    project = ProjectContext(canonical_id="pdr", root=str(tmp_path), task_source=FILA_PROJECT.task_source)
    base = _task(title="Base work")
    dependent = _task(title="Dependent work", dependencies=[base.id])
    plan_result = PlanResult(tasks=[dependent, base])

    numbers = materialize_plan(plan_result, project, store, client=client)

    assert numbers == []
    assert github.posts == []
    content = fila.read_text(encoding="utf-8")
    assert "Base work" in content
    assert "Dependent work (depende de: Base work)" in content
    assert content.index("Base work") < content.index("Dependent work")


def test_non_github_project_without_resolvable_path_does_not_crash(setup):
    store, github, client = setup
    project = ProjectContext(canonical_id="solo", root=None, task_source="None - single-operator project")
    plan_result = PlanResult(tasks=[_task()])

    assert materialize_plan(plan_result, project, store, client=client) == []
    assert github.posts == []


def test_negated_github_mention_is_not_read_as_using_github():
    # Real-world shape (project_context_index.json): a project can mention
    # "GitHub Issues" in the same breath as denying it applies, e.g. "...
    # Not GitHub Issues for this project." A naive substring check misreads
    # that as an affirmative match - this is a direct regression test for
    # the negation handling, since going through materialize_plan can't
    # distinguish "correctly detected as non-GitHub" from "no repository
    # configured" (both silently return no posts either way).
    project = ProjectContext(
        canonical_id="pdr", root=None, repository="owner/repo",
        task_source="Not GitHub Issues for this project - uses its own queue file.",
    )
    assert _uses_github_issues(project) is False


def test_unnegated_github_mention_is_read_as_using_github():
    project = ProjectContext(
        canonical_id="hub", root=None, repository="owner/repo",
        task_source="GitHub Issues (`gh issue list` in this repo) - the real work queue.",
    )
    assert _uses_github_issues(project) is True


def test_empty_plan_returns_empty_list(setup):
    store, github, client = setup
    assert materialize_plan(PlanResult(tasks=[]), GITHUB_PROJECT, store, client=client) == []
    assert github.posts == []
