"""Tests for orchestrator.task_queue.materialize_plan (issue #23)."""
from datetime import datetime, timedelta, timezone
import json
import subprocess

import pytest

from orchestrator.audit import query_audit
from orchestrator.config import OrchestratorConfig
from orchestrator.github_client import GitHubClient
from orchestrator.models import AgentClass, ExecutionMode, Task, TaskState
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult
from orchestrator.project_resolver import ProjectContext
from orchestrator.task_queue import _resolve_fallback_path, _uses_github_issues, materialize_plan


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class GitHub:
    """Fake `gh` subprocess - same shape as test_github_client.py's fake,
    duplicated locally so this suite has no cross-file test dependency.
    fail_titles lets a test force specific creations to fail (simulating
    a transient error) without touching production code."""

    def __init__(self, fail_titles=(), patch_error=None):
        self.issues, self.posts, self.patches = [], [], []
        self.fail_titles = set(fail_titles)
        self.patch_error = patch_error

    def run(self, args, **kwargs):
        method = args[args.index('--method') + 1]
        endpoint = args[args.index('--method') + 2]
        if method == 'GET':
            return subprocess.CompletedProcess(args, 0, json.dumps([self.issues]), '')
        if method == 'PATCH':
            number = int(endpoint.rsplit('/', 1)[-1])
            body = json.loads(kwargs['input'])
            self.patches.append({'number': number, **body})
            if self.patch_error:
                return subprocess.CompletedProcess(args, 1, '', self.patch_error)
            for issue in self.issues:
                if issue['number'] == number:
                    issue['body'] = body['body']
                    return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')
            return subprocess.CompletedProcess(args, 1, '', 'not found')
        body = json.loads(kwargs['input'])
        if body['title'] in self.fail_titles:
            # "connection refused" makes #17's client treat this as a
            # clean, definitely-not-sent failure (not "uncertain" - POST
            # might have landed anyway) so a later retry actually
            # re-attempts the POST instead of only checking for a
            # marker forever.
            return subprocess.CompletedProcess(args, 1, '', 'connection refused')
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

    # BLOCKS is backfilled in a second pass once every number is known.
    final_bodies = {issue['number']: issue['body'] for issue in github.issues}
    assert "BLOCKS: #2" in final_bodies[1]
    assert "BLOCKS: #3" in final_bodies[2]
    assert "BLOCKS: none" in final_bodies[3]


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


# --- Regressions from Codex's review (Review Task #117) -------------------

def test_partial_failure_never_publishes_dependent_as_free_then_replay_completes_it(tmp_path):
    # Finding #1: if the parent's creation fails, the dependent (and ITS
    # own dependents) must never be published with BLOCKED_BY: none - a
    # replay must then complete the graph correctly, and #17's dedup must
    # mean the already-succeeded creation is never re-posted.
    store = Store(tmp_path / 'state.db')
    github = GitHub(fail_titles={"Base work"})
    clock = Clock()
    client = GitHubClient(store, config=OrchestratorConfig(retry_interval_seconds=10),
                          run_fn=github.run, clock=clock, timeout_seconds=5)
    base = _task(title="Base work")
    dependent = _task(title="Dependent work", dependencies=[base.id])
    plan_result = PlanResult(tasks=[base, dependent])

    first = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)
    assert first == []  # base failed, dependent correctly deferred
    assert github.posts == []

    github.fail_titles.clear()
    clock.advance(11)  # past #17's retry backoff, so the retry actually re-attempts the POST
    second = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert second == [1, 2]
    assert [post['title'] for post in github.posts] == ["Base work", "Dependent work"]
    final_bodies = {issue['number']: issue['body'] for issue in github.issues}
    assert "BLOCKED_BY: none" in final_bodies[1]
    assert "BLOCKED_BY: #1" in final_bodies[2]
    store.close()


def test_cyclic_tasks_are_never_materialized_as_free_standing(setup):
    store, github, client = setup
    a = _task(title="A")
    b = _task(title="B", dependencies=[a.id])
    a.dependencies = [b.id]  # A <-> B cycle, not flagged BLOCKED by anything upstream
    plan_result = PlanResult(tasks=[a, b])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == []
    assert github.posts == []


def test_blocked_task_and_its_dependent_are_deferred_not_published_free(setup):
    store, github, client = setup
    blocked = _task(title="Blocked parent", state=TaskState.BLOCKED)
    dependent = _task(title="Dependent work", dependencies=[blocked.id])
    plan_result = PlanResult(tasks=[blocked, dependent])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == []
    assert github.posts == []


def test_external_dependency_confirmed_done_is_not_grounds_for_deferral(setup):
    # An id that isn't part of THIS batch but IS persisted and DONE is
    # genuinely resolved - it just can't be cited by a real Issue number
    # here (documented limitation), which is not a reason to defer.
    store, github, client = setup
    external = _task(title="External done work", state=TaskState.DONE)
    store.save_task(external)
    task = _task(title="Depends on external done work", dependencies=[external.id])
    plan_result = PlanResult(tasks=[task])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == [1]
    assert "BLOCKED_BY: none" in github.posts[0]['body']


def test_external_dependency_still_in_progress_defers_the_task(setup):
    # Regression (Review Task #117 round 2, finding #1): the planner's
    # own dedup can point a dependency at a persisted task that hasn't
    # finished - "not in this batch" must never be read as "resolved"
    # without real evidence.
    store, github, client = setup
    external = _task(title="External still in progress", state=TaskState.IN_PROGRESS)
    store.save_task(external)
    task = _task(title="Depends on unresolved external work", dependencies=[external.id])
    plan_result = PlanResult(tasks=[task])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == []
    assert github.posts == []


def test_external_dependency_that_does_not_exist_anywhere_defers_the_task(setup):
    # A missing task is exactly as unresolved as one that's merely still
    # in progress - never treated as silently satisfied.
    store, github, client = setup
    task = _task(title="Depends on nothing that exists", dependencies=["ghost-task-id"])
    plan_result = PlanResult(tasks=[task])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == []
    assert github.posts == []


def test_fallback_path_cannot_escape_project_root_via_relative_traversal(tmp_path):
    store = Store(tmp_path / 'state.db')
    outside = tmp_path / "outside.md"  # one level up from project_root below
    project_root = tmp_path / "repo"
    project_root.mkdir()
    project = ProjectContext(
        canonical_id="pdr", root=str(project_root),
        task_source=r"../outside.md in this repo - a live work queue. Not GitHub Issues for this project.",
    )
    plan_result = PlanResult(tasks=[_task()])

    numbers = materialize_plan(plan_result, project, store, client=None)

    assert numbers == []
    assert not outside.exists()
    store.close()


def test_fallback_path_cannot_escape_project_root_via_rooted_fragment(tmp_path):
    # A leading '/' fragment resolves outside root on every platform (on
    # Windows it lands on the current drive's own root, e.g. C:\etc\...;
    # on POSIX it becomes the literal absolute path) - asserted directly
    # against _resolve_fallback_path rather than guessing a platform-
    # specific escape location.
    project_root = tmp_path / "repo"
    project_root.mkdir()
    project = ProjectContext(
        canonical_id="pdr", root=str(project_root),
        task_source=r"/etc/cron.d/evil.md in this repo. Not GitHub Issues for this project.",
    )

    assert _resolve_fallback_path(project) is None

    store = Store(tmp_path / 'state.db')
    numbers = materialize_plan(PlanResult(tasks=[_task()]), project, store, client=None)
    assert numbers == []
    store.close()


def test_fallback_write_is_idempotent_across_replays(tmp_path):
    # Finding #3: two calls with the same PlanResult must not duplicate
    # the entry in the queue file.
    store = Store(tmp_path / 'state.db')
    (tmp_path / "docs" / "ai").mkdir(parents=True)
    fila = tmp_path / "docs" / "ai" / "FILA.md"
    fila.write_text("# Fila existente\n", encoding="utf-8")
    project = ProjectContext(canonical_id="pdr", root=str(tmp_path), task_source=FILA_PROJECT.task_source)
    task = _task(title="Solo fallback task")
    plan_result = PlanResult(tasks=[task])

    materialize_plan(plan_result, project, store, client=None)
    materialize_plan(plan_result, project, store, client=None)

    content = fila.read_text(encoding="utf-8")
    assert content.count("Solo fallback task") == 1
    store.close()


def test_blocks_backfill_preserves_correlation_marker_and_other_content(setup):
    # Regression (Review Task #117 round 2, finding #3): the first version
    # of the BLOCKS backfill regenerated the WHOLE body, silently erasing
    # #17's own jarvis-correlation marker (breaking its dedup on any later
    # replay) and any human-added content. The real GitHubClient embeds
    # the marker itself at POST time, so this exercises the real path.
    store, github, client = setup
    base = _task(title="Base work")
    dependent = _task(title="Dependent work", dependencies=[base.id])
    plan_result = PlanResult(tasks=[dependent, base])

    materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    base_issue = next(issue for issue in github.issues if issue['title'] == "Base work")
    assert "jarvis-correlation:" in base_issue['body']
    assert "BLOCKS: #2" in base_issue['body']
    assert "Do the thing" in base_issue['body']  # original objective text preserved

    # A replay must still be recognized as the same Issue (dedup depends
    # on the marker surviving the patch).
    second = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)
    assert second == [1, 2]
    assert len(github.posts) == 2  # no re-POST


def test_patch_failure_is_recorded_as_incomplete_not_silently_dropped(tmp_path):
    store = Store(tmp_path / 'state.db')
    github = GitHub(patch_error="server error")
    client = GitHubClient(store, config=OrchestratorConfig(retry_interval_seconds=10),
                          run_fn=github.run, clock=Clock(), timeout_seconds=5)
    base = _task(title="Base work")
    dependent = _task(title="Dependent work", dependencies=[base.id])
    plan_result = PlanResult(tasks=[dependent, base])

    numbers = materialize_plan(plan_result, GITHUB_PROJECT, store, client=client)

    assert numbers == [1, 2]  # both still created - only the relationship backfill failed
    entries = query_audit(store)
    incomplete = [e for e in entries if e['action'] == 'materialize_relationships' and e['result'] == 'incomplete']
    assert len(incomplete) == 1
    assert incomplete[0]['extra']['issue_number'] == 1
    store.close()


def test_fallback_write_survives_crash_between_append_and_idempotency_record(tmp_path):
    # Regression (Review Task #117 round 2, finding #2): a crash right
    # after the file append but before record_idempotency_key used to
    # duplicate the entry on the next replay. Simulated here by writing
    # the block directly (bypassing the DB write) and then replaying
    # through materialize_plan - the in-file marker must be what a replay
    # actually trusts, not just the DB key.
    store = Store(tmp_path / 'state.db')
    (tmp_path / "docs" / "ai").mkdir(parents=True)
    fila = tmp_path / "docs" / "ai" / "FILA.md"
    fila.write_text("# Fila existente\n", encoding="utf-8")
    project = ProjectContext(canonical_id="pdr", root=str(tmp_path), task_source=FILA_PROJECT.task_source)
    task = _task(title="Crash recovery task")
    plan_result = PlanResult(tasks=[task])

    # First call succeeds normally (both file write and DB record happen).
    materialize_plan(plan_result, project, store, client=None)
    assert fila.read_text(encoding="utf-8").count("Crash recovery task") == 1

    from orchestrator.task_queue import _fallback_marker
    assert _fallback_marker(task.correlation_id) in fila.read_text(encoding="utf-8")

    # Simulate the crash directly: the DB write that should have followed
    # the (already-landed) file append never happened. A replay must
    # still recognize the task via the in-file marker alone, not the
    # (now-missing) idempotency key.
    store.execute(
        "DELETE FROM idempotency_keys WHERE correlation_id = ? AND kind = ?",
        (task.correlation_id, f"fallback_queue:{fila.resolve()}"),
    )

    materialize_plan(plan_result, project, store, client=None)
    assert fila.read_text(encoding="utf-8").count("Crash recovery task") == 1
    store.close()


def test_fallback_defers_dependent_of_needs_lucas_task(tmp_path):
    store = Store(tmp_path / 'state.db')
    (tmp_path / "docs" / "ai").mkdir(parents=True)
    fila = tmp_path / "docs" / "ai" / "FILA.md"
    fila.write_text("# Fila existente\n", encoding="utf-8")
    project = ProjectContext(canonical_id="pdr", root=str(tmp_path), task_source=FILA_PROJECT.task_source)
    needs_human = _task(title="Needs a human", state=TaskState.NEEDS_LUCAS)
    dependent = _task(title="Waits on human decision", dependencies=[needs_human.id])
    plan_result = PlanResult(tasks=[needs_human, dependent])

    materialize_plan(plan_result, project, store, client=None)

    content = fila.read_text(encoding="utf-8")
    assert "Needs a human" not in content
    assert "Waits on human decision" not in content
    store.close()
