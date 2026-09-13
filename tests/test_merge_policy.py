"""Tests for orchestrator.merge_policy (issue #37). No real gh/network
call - run_fn is always an injected fake.

Several scenarios here are regressions from Codex's review of the first
version (Review Task #111): unsupported `gh pr view` JSON fields, a merge
that ignored whether the PR's head still matched what was reviewed, an
unchecked base/draft/up-to-date state, a duplicate-check-name masking bug
in `_checks_green`, and trusting `gh pr merge`'s own exit code as proof of
completion instead of re-reading the PR.
"""
import subprocess

import pytest

from orchestrator.audit import query_audit
from orchestrator.events import EventType, emit, query_events
from orchestrator.merge_policy import MergeOutcome, try_auto_merge
from orchestrator.models import Task
from orchestrator.persistence import Store

HEAD = "a" * 40
REPO, BASE = "org/repo", "integration/orchestration"


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _rollup(*, pytest_ok=True, backdate_ok=False):
    return [
        {"name": "pytest", "conclusion": "SUCCESS" if pytest_ok else "FAILURE", "status": "COMPLETED"},
        {"name": "backdate", "conclusion": "SUCCESS" if backdate_ok else "FAILURE", "status": "COMPLETED"},
    ]


def _view_json(*, state="OPEN", is_draft=False, base=BASE, head=HEAD,
              merge_state="CLEAN", rollup=None, merge_commit=None):
    import json
    return json.dumps({
        "state": state, "isDraft": is_draft, "baseRefName": base, "headRefOid": head,
        "mergeStateStatus": merge_state,
        "statusCheckRollup": rollup if rollup is not None else _rollup(),
        "mergeCommit": merge_commit,
    })


def _pass_review(store, task, *, head_sha=HEAD, repo=REPO, pr_number=42,
                 task_id_override=None):
    payload = {"task_id": task_id_override if task_id_override is not None else task.id}
    if head_sha is not None:
        payload["head_sha"] = head_sha
    if repo is not None:
        payload["repo"] = repo
    if pr_number is not None:
        payload["pr_number"] = pr_number
    emit(store, EventType.REVIEW_PASSED, payload, correlation_id=task.correlation_id)


def _fail_review(store, task):
    emit(store, EventType.REVIEW_FAILED, {"task_id": task.id}, correlation_id=task.correlation_id)


def _merge(task, store, *, repo=REPO, **overrides):
    kwargs = dict(expected_head_sha=HEAD, expected_base=BASE, required_checks=["pytest"])
    kwargs.update(overrides)
    return try_auto_merge(task, repo, 42, store, **kwargs)


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
    views = iter([_view_json(state="OPEN"), _view_json(state="MERGED")])

    def run_fn(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=next(views))
        return _Result()

    result = _merge(task, store, run_fn=run_fn)

    assert result == MergeOutcome(merged=True, reason="merged")
    merge_call = next(c for c in calls if c[1:3] == ["pr", "merge"])
    assert merge_call == ["gh", "pr", "merge", "42", "--repo", REPO, "--merge",
                          "--match-head-commit", HEAD]
    events = query_events(store, event_types=[EventType.MERGE_COMPLETED])
    assert len(events) == 1
    assert events[0]["payload"]["head_sha"] == HEAD
    entries = query_audit(store)
    assert entries[-1]["result"] == "success"
    store.close()


def test_does_not_merge_when_review_not_passed(store, task):
    calls = []

    def run_fn(args, **kwargs):
        calls.append(args)
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)

    assert result.merged is False
    assert result.reason == "review_not_passed"
    assert calls == []  # never even queries the PR
    store.close()


def test_last_failed_review_overrides_earlier_pass(store, task):
    _pass_review(store, task)
    _fail_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_does_not_merge_when_ci_not_green(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json(rollup=_rollup(pytest_ok=False)))
        return _Result()

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="ci_not_green")
    store.close()


def test_without_required_checks_backdate_failure_blocks_merge(store, task):
    # Default (no required_checks given) demands EVERY reported check be
    # green - the caller must explicitly opt into ignoring a known-legacy
    # check like backdate, never silently baked into this module.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=_rollup(pytest_ok=True, backdate_ok=False)))

    result = _merge(task, store, required_checks=None, run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_duplicate_check_name_all_instances_must_be_green(store, task):
    # Regression (Review Task #111, finding #4): two runs named "pytest"
    # (e.g. a push trigger and a PR trigger) - a FAILURE followed by a
    # later SUCCESS under the SAME name must never be read as green.
    _pass_review(store, task)
    rollup = [
        {"name": "pytest", "conclusion": "FAILURE", "status": "COMPLETED"},
        {"name": "pytest", "conclusion": "SUCCESS", "status": "COMPLETED"},
    ]

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=rollup))

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_incomplete_check_status_is_not_green(store, task):
    _pass_review(store, task)
    rollup = [{"name": "pytest", "conclusion": None, "status": "IN_PROGRESS"}]

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=rollup))

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_does_not_merge_on_stale_base(store, task):
    # Regression (finding #3): MERGEABLE-ish state that isn't actually
    # up-to-date against its base must never merge.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(merge_state="BEHIND"))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merge_state_not_clean")
    store.close()


def test_does_not_merge_dirty_state(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(merge_state="DIRTY"))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merge_state_not_clean")
    store.close()


def test_does_not_merge_draft_pr(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(is_draft=True))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="pr_is_draft")
    store.close()


def test_does_not_merge_unexpected_base(store, task):
    # Regression (finding #3): a task's PR must never merge into main or
    # the frozen wave-0 checkpoint just because it happens to be clean.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(base="main"))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="unexpected_base")
    store.close()


def test_does_not_merge_when_head_moved_since_review(store, task):
    # Regression (finding #2): a PASS must not authorize merging whatever
    # commit happens to be on the PR NOW if it moved since the review.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(head="b" * 40))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="head_mismatch")
    store.close()


def test_does_not_merge_closed_unmerged_pr(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(state="CLOSED"))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="pr_not_open")
    store.close()


def test_already_merged_on_first_observation_reconciles_completion(store, task):
    # Regression (finding #2): the ORIGINAL version never reconciled
    # completion on this path at all (a crash between a successful
    # remote merge and recording it left this branch permanently silent
    # on retry). The first time this branch is reached, it must record
    # the completion - never re-attempts the merge command itself.
    _pass_review(store, task)
    calls = []

    def run_fn(args, **kwargs):
        calls.append(args)
        return _Result(stdout=_view_json(state="MERGED"))

    result = _merge(task, store, run_fn=run_fn)

    assert result == MergeOutcome(merged=True, reason="merged", already_merged=False)
    assert all(c[1:3] != ["pr", "merge"] for c in calls)
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
    store.close()


def test_calling_twice_on_already_merged_reconciles_once_then_is_idempotent(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(state="MERGED"))

    first = _merge(task, store, run_fn=run_fn)
    second = _merge(task, store, run_fn=run_fn)
    assert first == MergeOutcome(merged=True, reason="merged", already_merged=False)
    assert second == MergeOutcome(merged=True, reason="already_merged", already_merged=True)
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
    store.close()


def test_repo_case_does_not_duplicate_merge_completion(store, task):
    # Regression (Review Task #111 round 4): _review_passed compares repo
    # case-insensitively, but the idempotency key and the recorded
    # event/audit used the caller's raw casing. Calling with "org/repo"
    # then "Org/Repo" for the SAME PR/head - both authorized by the same
    # PASS - used to build two distinct run_sync_once keys and record two
    # MERGE_COMPLETED events + two success audits for one real merge.
    _pass_review(store, task, repo=REPO)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(state="MERGED"))

    first = _merge(task, store, repo="org/repo", run_fn=run_fn)
    second = _merge(task, store, repo="Org/Repo", run_fn=run_fn)
    assert first == MergeOutcome(merged=True, reason="merged", already_merged=False)
    assert second == MergeOutcome(merged=True, reason="already_merged", already_merged=True)
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
    success_entries = [e for e in query_audit(store) if e["result"] == "success"]
    assert len(success_entries) == 1
    store.close()


def test_merge_command_failure_is_reported_not_raised(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json())
        return _Result(returncode=1, stderr="merge failed")

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merge_command_failed")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()


def test_zero_exit_without_confirmed_merge_is_not_reported_as_merged(store, task):
    # Regression (finding #5): `gh pr merge` can exit 0 having only
    # QUEUED the merge (see `gh pr merge --help`) - this must never be
    # read as success, and must never emit MERGE_COMPLETED, until the PR
    # is re-read and actually shows state == MERGED.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json(state="OPEN"))
        return _Result(returncode=0)  # merge command claims success

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merge_queued_unconfirmed")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()


def test_confirmed_merge_after_command_records_completion_exactly_once_on_retry(store, task):
    # A crash/timeout between a successful merge and recording it must be
    # safely retryable without double-emitting MERGE_COMPLETED.
    _pass_review(store, task)
    views = iter([_view_json(state="OPEN"), _view_json(state="MERGED")])

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=next(views))
        return _Result(returncode=0)

    first = _merge(task, store, run_fn=run_fn)
    assert first == MergeOutcome(merged=True, reason="merged")

    # Re-run the exact same (repo, pr, head) completion path again - must
    # not duplicate the event even though this call also "confirms merged"
    # independently (simulating a retry after the first call's own event
    # write crashed before returning).
    def run_fn_retry(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=_view_json(state="MERGED"))
        return _Result(returncode=0)

    # By the time of the retry, the PR already shows MERGED on the very
    # first read - the early "already_merged" short-circuit takes over,
    # which is itself the idempotency guarantee: it never re-attempts the
    # merge command and never touches the completion-recording path again.
    second = _merge(task, store, run_fn=run_fn_retry)
    assert second == MergeOutcome(merged=True, reason="already_merged", already_merged=True)
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
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

    result = _merge(task, store, run_fn=run_fn)
    assert result.merged is False
    store.close()


@pytest.mark.parametrize("payload", ["not json", "[]", "null"])
def test_malformed_pr_view_response_does_not_merge(store, task, payload):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=payload)

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="invalid_pr_response")
    store.close()


def test_missing_check_names_in_rollup_are_treated_as_not_green(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=[{"name": "pytest", "conclusion": "SUCCESS"}]))

    result = _merge(task, store, required_checks=["pytest", "lint"], run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_empty_rollup_is_never_treated_as_green(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(rollup=[]))

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "ci_not_green"
    store.close()


def test_every_decision_path_is_audit_logged(store, task):
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(base="main"))

    _merge(task, store, run_fn=run_fn)
    entries = query_audit(store)
    assert len(entries) == 1
    assert entries[0]["action"] == "auto_merge"
    assert entries[0]["extra"]["reason"] == "unexpected_base"
    store.close()


# --- Regressions from Codex's round-2 review (Review Task #111) -----------

def test_pass_with_no_head_sha_recorded_is_not_an_attestation(store, task):
    # Finding #1: a caller-supplied expected_head_sha that merely gets
    # compared to the PR's current head does not prove THAT commit was
    # what got reviewed - a PASS with no head_sha attached (an older
    # caller, or one that couldn't supply it) must not authorize a merge.
    _pass_review(store, task, head_sha=None)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_pass_recorded_for_a_different_commit_is_not_an_attestation_for_this_one(store, task):
    # A PASS event for head 'b' must not authorize merging head 'a', even
    # if the CALLER (mistakenly or maliciously) passes expected_head_sha='a'
    # and the PR's current head genuinely is 'a' - the attestation itself
    # has to be for the commit being merged, not just consistent with it.
    _pass_review(store, task, head_sha="b" * 40)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(head=HEAD))

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_already_merged_with_different_head_is_never_claimed_as_this_tasks_success(store, task):
    # Finding #3: GitHub already reports the PR merged, but its head
    # doesn't match what we expected - a different commit got merged
    # (race, or a stale call against a PR that moved on since). Must
    # never be silently reported as this task's own success.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(state="MERGED", head="c" * 40))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merged_different_head")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()


def test_confirmed_merge_with_different_final_head_is_never_claimed_as_success(store, task):
    # Finding #3, the other path: our OWN merge command path confirms
    # state==MERGED, but the final head doesn't match what we intended -
    # --match-head-commit should refuse this in practice, but the
    # confirmation checks independently rather than trusting that alone.
    _pass_review(store, task)
    views = iter([_view_json(state="OPEN"), _view_json(state="MERGED", head="c" * 40)])

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=next(views))
        return _Result(returncode=0)

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merged_different_head")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()


def test_crash_before_recording_is_recovered_via_the_already_merged_path(store, task):
    # Finding #2: the ORIGINAL fix only reconciled completion on the path
    # where THIS call performed the merge - a crash between a successful
    # remote merge (by an EARLIER call, or another actor) and recording
    # it left the already-merged path permanently silent forever on
    # retry. Simulates that earlier crash by never running the merge
    # command in this test at all - GitHub already shows it merged from
    # the very first observation, and completion still gets recorded.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(state="MERGED"))

    result = _merge(task, store, run_fn=run_fn)

    assert result.merged is True
    events = query_events(store, event_types=[EventType.MERGE_COMPLETED])
    assert len(events) == 1
    entries = query_audit(store)
    assert any(e["action"] == "auto_merge" and e["result"] == "success" for e in entries)
    store.close()


# --- Regressions from Codex's round-3 review (Review Task #111) -----------

def test_pass_for_a_different_task_is_not_an_attestation_for_this_one(store, task):
    # Round 3, finding #1: a SHA match alone isn't unique enough - a PASS
    # explicitly recorded for a DIFFERENT task_id must not authorize this
    # task's merge even if head/repo/pr_number all happen to coincide.
    _pass_review(store, task, task_id_override="some-other-task-id")

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_pass_for_a_different_repo_is_not_an_attestation_for_this_one(store, task):
    _pass_review(store, task, repo="org/other-repo")

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_pass_for_a_different_pr_is_not_an_attestation_for_this_one(store, task):
    _pass_review(store, task, pr_number=99)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_pass_missing_repo_or_pr_number_is_not_a_full_attestation(store, task):
    # An older caller (or one for a task with no PR) that only supplied
    # head_sha must not authorize auto-merge either - full identity is
    # required, not just whichever fields happen to be present.
    _pass_review(store, task, repo=None, pr_number=None)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    result = _merge(task, store, run_fn=run_fn)
    assert result.reason == "review_not_passed"
    store.close()


def test_repo_comparison_is_case_insensitive(store, task):
    _pass_review(store, task, repo=REPO.upper())
    views = iter([_view_json(state="OPEN"), _view_json(state="MERGED")])

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=next(views))
        return _Result()

    result = _merge(task, store, run_fn=run_fn)
    assert result.merged is True
    store.close()


def test_already_merged_into_wrong_base_is_never_claimed_as_success(store, task):
    # Round 3, finding #2: GitHub already reports MERGED with a matching
    # head, but into the WRONG base (e.g. main instead of the intended
    # integration checkpoint) - must never be claimed as this task's own
    # completion, on either MERGED-observation path.
    _pass_review(store, task)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json(state="MERGED", base="main"))

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merged_unexpected_base")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()


def test_confirmed_merge_into_wrong_base_is_never_claimed_as_success(store, task):
    _pass_review(store, task)
    views = iter([_view_json(state="OPEN"), _view_json(state="MERGED", base="main")])

    def run_fn(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            return _Result(stdout=next(views))
        return _Result(returncode=0)

    result = _merge(task, store, run_fn=run_fn)
    assert result == MergeOutcome(merged=False, reason="merged_unexpected_base")
    assert query_events(store, event_types=[EventType.MERGE_COMPLETED]) == []
    store.close()
