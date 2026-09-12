"""Automatic PR merge policy (issue #37).

Merges a PR automatically only when ALL of the following hold: the task's
last recorded review verdict (#29's REVIEW_PASSED/REVIEW_FAILED events) is
PASS, the PR is open, not a draft, targets the expected base, its current
head matches the exact commit the caller says was reviewed, GitHub reports
it clean/up-to-date against that base, and its required CI checks are
green. A failed/blocked check is a normal "don't merge yet" outcome, never
an automatic escalation to Lucas - #29 already owns that escalation
policy, and this module's OUT OF SCOPE explicitly excludes resolving merge
conflicts (that stays the implementing agent's normal job).

`expected_head_sha` and `expected_base` are supplied by the caller (the
runtime that requested/received the review) rather than inferred here:
#29's REVIEW_PASSED event only carries task_id+reviewer, not which exact
commit or PR was reviewed, so this module cannot on its own tell a stale
review apart from a fresh one. Requiring the caller to state - and this
module to verify against the PR's live head - closes the most dangerous
gap (a new push after review still gets merged unreviewed) without this
issue reaching into #29's event schema. Binding the review verdict itself
to a specific commit/PR would need to happen there, if ever required.

Every decision (merged, skipped, or failed) is recorded via #20's audit
log, per the issue's own risk-mitigation note (RISK: Alto). Completing a
merge is recorded (MERGE_COMPLETED) at most once per (repo, PR, head sha)
via #13's run_sync_once, so a retry or a concurrent caller after a
crash/timeout never double-emits.

All GitHub calls go through `gh` CLI via subprocess with argument arrays,
same pattern as #17's github_client.py - no shell involved, still fully
injectable for tests. This module never calls `gh` with a real network
connection during its own test suite. A `gh pr merge` exit code alone is
never trusted as proof of completion (per `gh pr merge --help`, it can
mean "queued") - the PR is always re-read afterward and only a confirmed
`state == "MERGED"` counts as success.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from orchestrator.audit import record as audit_record
from orchestrator.events import EventType, emit_in_transaction, query_events
from orchestrator.models import Task
from orchestrator.persistence import Store

_PR_VIEW_FIELDS = "state,mergeStateStatus,isDraft,baseRefName,headRefOid,statusCheckRollup,mergeCommit"


class _Failure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class MergeOutcome:
    merged: bool
    reason: str
    already_merged: bool = False


def _review_passed(store: Store, task: Task) -> bool:
    """True if the LAST review verdict recorded for this task (by its
    correlation_id) was PASS - a later REVIEW_FAILED always overrides an
    earlier PASS, and no verdict at all is never treated as a pass."""
    events = query_events(
        store, correlation_id=task.correlation_id,
        event_types=[EventType.REVIEW_PASSED, EventType.REVIEW_FAILED],
    )
    if not events:
        return False
    return events[-1]["event_type"] == EventType.REVIEW_PASSED


def _pr_view(repo: str, pr_number: int, run_fn: Callable, timeout: float) -> dict:
    args = ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", _PR_VIEW_FIELDS]
    try:
        result = run_fn(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise _Failure("gh_not_installed") from None
    except subprocess.TimeoutExpired:
        raise _Failure("timeout") from None
    except OSError:
        raise _Failure("process_unavailable") from None
    if result.returncode != 0:
        raise _Failure("pr_view_failed")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise _Failure("invalid_pr_response") from None
    if not isinstance(data, dict):
        raise _Failure("invalid_pr_response")
    return data


def _checks_green(rollup, required_checks: list[str] | None) -> bool:
    """Every reported run for each required check name must be SUCCESS
    (and COMPLETED where a status is reported) - a repo commonly reports
    the SAME check name more than once (e.g. a push trigger and a PR
    trigger both named "pytest"); grouping by name and requiring ALL
    instances green (not just the last one seen) stops a red run from
    being masked by a later green one under the same name."""
    if not isinstance(rollup, list) or not rollup:
        return False
    by_name: dict[str, list[dict]] = {}
    for entry in rollup:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            return False
        by_name.setdefault(entry["name"], []).append(entry)

    names = required_checks if required_checks is not None else sorted(by_name)
    if not names:
        # No checks configured/reported at all is not proof of a green
        # build - never merge on the absence of evidence.
        return False
    for name in names:
        entries = by_name.get(name)
        if not entries:
            return False
        for entry in entries:
            status = entry.get("status")
            if status is not None and status != "COMPLETED":
                return False
            if entry.get("conclusion") != "SUCCESS":
                return False
    return True


def _merge_pr(repo: str, pr_number: int, expected_head_sha: str, run_fn: Callable, timeout: float) -> bool:
    """Attempts the merge; returns whether `gh` itself reported success.
    Callers must NOT treat a True return as proof of completion - re-read
    the PR afterward (see module docstring)."""
    args = [
        "gh", "pr", "merge", str(pr_number), "--repo", repo, "--merge",
        "--match-head-commit", expected_head_sha,
    ]
    try:
        result = run_fn(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise _Failure("gh_not_installed") from None
    except subprocess.TimeoutExpired:
        raise _Failure("timeout") from None
    except OSError:
        raise _Failure("process_unavailable") from None
    return result.returncode == 0


def try_auto_merge(
    task: Task,
    repo: str,
    pr_number: int,
    store: Store,
    *,
    expected_head_sha: str,
    expected_base: str,
    required_checks: list[str] | None = None,
    run_fn: Callable | None = None,
    timeout: float = 30,
) -> MergeOutcome:
    """Merges `pr_number` into `expected_base` if review passed, the PR's
    current head is exactly `expected_head_sha`, GitHub reports it open,
    non-draft, clean/up-to-date against that base, and CI is green (per
    `required_checks`, or every reported check when omitted). Idempotent:
    calling this twice on an already-merged PR never re-attempts the merge
    or raises, and completion is recorded at most once per (repo, PR,
    head sha) even across retries or a crash between merging and
    recording.
    """
    run = run_fn or subprocess.run

    def _fail(reason: str, *, result: str = "skipped") -> MergeOutcome:
        outcome = MergeOutcome(merged=False, reason=reason)
        audit_record(store, action="auto_merge", origin="merge_policy", result=result,
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": reason})
        return outcome

    if not _review_passed(store, task):
        return _fail("review_not_passed")

    try:
        pr = _pr_view(repo, pr_number, run, timeout)
    except _Failure as error:
        return _fail(error.reason, result="failed")

    if pr.get("state") == "MERGED":
        outcome = MergeOutcome(merged=True, reason="already_merged", already_merged=True)
        audit_record(store, action="auto_merge", origin="merge_policy", result="skipped",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    if pr.get("state") != "OPEN":
        return _fail("pr_not_open")
    if pr.get("isDraft"):
        return _fail("pr_is_draft")
    if pr.get("baseRefName") != expected_base:
        return _fail("unexpected_base")
    if pr.get("headRefOid") != expected_head_sha:
        return _fail("head_mismatch")
    if pr.get("mergeStateStatus") != "CLEAN":
        return _fail("merge_state_not_clean")
    if not _checks_green(pr.get("statusCheckRollup"), required_checks):
        return _fail("ci_not_green")

    try:
        merge_command_succeeded = _merge_pr(repo, pr_number, expected_head_sha, run, timeout)
    except _Failure as error:
        return _fail(error.reason, result="failed")

    # A zero exit from `gh pr merge` can mean "queued", not "merged"; a
    # nonzero exit can race with GitHub (or another caller) completing the
    # merge anyway. Only a fresh, confirmed state == MERGED counts.
    try:
        confirmed = _pr_view(repo, pr_number, run, timeout)
    except _Failure as error:
        return _fail(error.reason, result="failed")

    if confirmed.get("state") != "MERGED":
        if merge_command_succeeded:
            return _fail("merge_queued_unconfirmed")
        return _fail("merge_command_failed", result="failed")

    key = f"merge_policy:{repo}:{pr_number}:{expected_head_sha}"

    def record(connection):
        emit_in_transaction(
            connection, EventType.MERGE_COMPLETED,
            {"repo": repo, "pr_number": pr_number, "task_id": task.id,
             "head_sha": expected_head_sha,
             "merge_commit": (confirmed.get("mergeCommit") or {}).get("oid")},
            correlation_id=task.correlation_id, project_id=task.project_id,
        )

    newly_recorded = store.run_sync_once(key, "merged", record)
    outcome = MergeOutcome(merged=True, reason="merged")
    audit_record(store, action="auto_merge", origin="merge_policy",
                 result="success" if newly_recorded else "skipped",
                 correlation_id=task.correlation_id, project_id=task.project_id,
                 extra={"pr_number": pr_number, "repo": repo,
                        "reason": "merged" if newly_recorded else "already_recorded"})
    return outcome
