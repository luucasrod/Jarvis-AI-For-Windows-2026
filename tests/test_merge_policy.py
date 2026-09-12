"""Tests for orchestrator.merge_policy (issue #37). No real gh/network
call - run_fn is always an injected fake."""
import subprocess

import pytest

from orchestrator.audit import query_audit
from orchestrator.events import EventType, emit, query_events
from orchestrator.merge_policy import MergeOutcome, try_auto_merge
from orchestrator.models import Task
from orchestrator.persistence import Store


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _rollup(*, pytest_ok=True, backdate_ok=False):
    return [
        {"name": "pytest", "conclusion": "SUCCESS" if pytest_ok else "FAILURE"},
        {"name": "backdate", "conclusion": "SUCCESS" if backdate_ok else "FAILURE"},
    ]


def _view_json(*, merged=False, mergeable="MERGEABLE", rollup=None):
    import json
    return json.dumps({
        "merged": merged, "mergeable": mergeable,
        "statusCheckRollup": rollup if rollup is not None else _rollup(),
    })


def _pass_review(store, task):
    emit(store, EventType.REVIEW_PASSED, {"task_id": task.id}, correlation_id=task.correlation_id)


def _fail_review(store, task):
    emit(store, EventType.REVIEW_FAILED, {"task_id": task.id}, correlation_id=task.correlation_id)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


@pytest.fixture
def task():
    return Task(title="Fix bug", objective="obj", project_id="jarvis")


def test_merges_when_review_passed_ci_green_and_mergeable(store, task):
    _pass_review(store, task)
    calls = []

    def run_fn(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json())
        return _Result()

    result = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)

    assert result == MergeOutcome(merged=True, reason="merged")
    assert calls[-1] == ["gh", "pr", "merge", "42", "--repo", "org/repo", "--merge"]
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
    entries = query_audit(store)
    assert entries[-1]["result"] == "success"
    store.close()


def test_does_not_merge_when_review_not_passed(store, task):
    calls = []

    def run_fn(args, **kwargs):
        calls.append(args)
        return _Result(stdout=_view_json())

    result = try_auto_merge(task, "org/repo", 42, store, run_fn=run_fn)

    assert result.merged is False
    assert result.reason == "review_not_passed"
    assert calls == []  # never even queries the PR
    store.close()


def test_last_failed_review_overrides_earlier_pass(store, task):
    _pass_review(store, task)
    _fail_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = try_auto_merge(task, "org/repo", 42, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_does_not_merge_when_ci_not_green(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json(rollup=_rollup(pytest_ok=False)))
        return _Result()

    result = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="ci_not_green")
    store.close()


def test_without_required_checks_backdate_failure_blocks_merge(store, task):
    # Default (no required_checks given) demands EVERY reported check be
    # green - the caller must explicitly opt into ignoring a known-legacy
    # check like backdate, never silently baked into this module.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=_rollup(pytest_ok=True, backdate_ok=False)))

    result = try_auto_merge(task, "org/repo", 42, store, run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_does_not_merge_on_conflict(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(mergeable="CONFLICTING"))

    result = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merge_conflict")
    store.close()


def test_already_merged_is_idempotent_and_does_not_call_merge(store, task):
    _pass_review(store, task)
    calls = []

    def run_fn(args, **kwargs):
        calls.append(args)
        return _Result(stdout=_view_json(merged=True))

    result = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)

    assert result == MergeOutcome(merged=True, reason="already_merged", already_merged=True)
    assert all(c[1:3] != ["pr", "merge"] for c in calls)
    store.close()


def test_calling_twice_on_already_merged_never_errors(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(merged=True))

    first = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)
    second = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)
    assert first == second == MergeOutcome(merged=True, reason="already_merged", already_merged=True)
    store.close()


def test_merge_command_failure_is_reported_not_raised(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json())
        return _Result(returncode=1, stderr="merge failed")

    result = try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merge_command_failed")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="gh", timeout=30),
    FileNotFoundError(),
    OSError("boom"),
])
def test_pr_view_transport_errors_do_not_raise(store, task, exc):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        raise exc

    result = try_auto_merge(task, "org/repo", 42, store, run_fn=run_fn)
    assert result.merged is False
    store.close()


@pytest.mark.parametrize("payload", ["not json", "[]", "null"])
def test_malformed_pr_view_response_does_not_merge(store, task, payload):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=payload)

    result = try_auto_merge(task, "org/repo", 42, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="invalid_pr_response")
    store.close()


def test_missing_check_names_in_rollup_are_treated_as_not_green(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=[{"name": "pytest", "conclusion": "SUCCESS"}]))

    result = try_auto_merge(
        task, "org/repo", 42, store, required_checks=["pytest", "lint"], run_fn=run_fn
    )
    assert result.reason == "ci_not_green"
    store.close()


def test_empty_rollup_is_never_treated_as_green(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=[]))

    result = try_auto_merge(task, "org/repo", 42, store, run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_every_decision_path_is_audit_logged(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(mergeable="CONFLICTING"))

    try_auto_merge(task, "org/repo", 42, store, required_checks=["pytest"], run_fn=run_fn)
    entries = query_audit(store)
    assert len(entries) == 1
    assert entries[0]["action"] == "auto_merge"
    assert entries[0]["extra"]["reason"] == "merge_conflict"
    store.close()
