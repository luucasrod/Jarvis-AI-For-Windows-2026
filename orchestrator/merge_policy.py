"""Automatic PR merge policy (issue #37).

Merges a PR automatically only when ALL of the following hold: the task's
last recorded review verdict (#29's REVIEW_PASSED/REVIEW_FAILED events) is
PASS *and was recorded against this exact commit*, the PR is open, not a
draft, targets the expected base, its current head matches that same
commit, GitHub reports it clean/up-to-date against that base, and its
required CI checks are green. A failed/blocked check is a normal "don't
merge yet" outcome, never an automatic escalation to Lucas - #29 already
owns that escalation policy, and this module's OUT OF SCOPE explicitly
excludes resolving merge conflicts (that stays the implementing agent's
normal job).

`expected_head_sha` and `expected_base` are supplied by the caller (the
runtime that requested/received the review). The review attestation
itself is no longer caller-supplied alone: #29's request_review() now
accepts optional `head_sha`, `repo` and `pr_number`, recorded together on
REVIEW_PASSED (Review Task #111, round 1 finding #1 - a caller-supplied
expected_head_sha that merely gets compared to the PR's current head
does not prove THAT commit was what got reviewed). `_review_passed` here
requires the last REVIEW_PASSED event's own recorded task_id, repo,
pr_number AND head_sha to ALL match this call's - a commit SHA alone is
not unique across forks, unrelated repos, or different base branches of
the same PR (round 3, finding #1: three independent reproductions showed
a PASS explicitly recorded for a different task/repo/PR still
authorizing a merge whenever the SHA happened to coincide). A PASS
missing any of these fields (an older caller, or one for a task with no
PR) is treated as no attestation for auto-merge, not as a pass.

Every decision (merged, skipped, or failed) is recorded via #20's audit
log, per the issue's own risk-mitigation note (RISK: Alto). A completed
merge's event AND audit record are written together, atomically, via
#13's run_sync_once + #20's record_in_transaction, keyed on (repo, PR,
head sha) - reached from BOTH "GitHub already reports this merged" and
"we just merged it and confirmed" (round 2, finding #2: the first
version only reconciled completion on the path where THIS call performed
the merge - a crash between a successful remote merge and recording it
left the already-merged path forever silent on retry). Both paths verify
the merged PR's base AND head actually match `expected_base`/
`expected_head_sha` before ever reporting success - base was checked
only on the not-yet-merged branch until round 3, finding #2 showed a PR
already merged into the wrong target (e.g. main instead of the
integration checkpoint) with a matching head still being reported as
success. A base mismatch reports `merged_unexpected_base`; a head
mismatch reports `merged_different_head` - neither is ever silently
claimed as this task's own completion.

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
from orchestrator.audit import record_in_transaction as audit_record_in_transaction
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


def _review_passed(store: Store, task: Task, repo: str, pr_number: int, expected_head_sha: str) -> bool:
    """True only if the LAST review verdict recorded for this task (by
    its correlation_id) was PASS *for this exact task/repo/PR/commit* - a
    later REVIEW_FAILED always overrides an earlier PASS, no verdict at
    all is never treated as a pass, and a PASS missing any of these
    fields (an older caller, or one for a task with no PR) is not a full
    attestation and is likewise never treated as a pass for auto-merge.

    A commit SHA alone is not unique across forks, across unrelated
    repos, or across different base branches of the same repo/PR - binding
    task_id + repo (case-insensitively, matching #17's own normalization)
    + pr_number closes the gap a SHA-only check left open (Review Task
    #111, round 3, finding #1): three independent reproductions showed a
    PASS explicitly recorded for a different task, repo, or PR still
    authorizing a merge whenever the SHA happened to coincide."""
    events = query_events(
        store, correlation_id=task.correlation_id,
        event_types=[EventType.REVIEW_PASSED, EventType.REVIEW_FAILED],
    )
    if not events:
        return False
    last = events[-1]
    if last["event_type"] != EventType.REVIEW_PASSED:
        return False
    payload = last["payload"]
    return (
        payload.get("task_id") == task.id
        and payload.get("head_sha") == expected_head_sha
        and isinstance(payload.get("repo"), str) and payload["repo"].lower() == repo.lower()
        and payload.get("pr_number") == pr_number
    )


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
    """Merges `pr_number` into `expected_base` if review passed FOR
    `expected_head_sha` specifically, the PR's current head is exactly
    that commit, GitHub reports it open, non-draft, clean/up-to-date
    against that base, and CI is green (per `required_checks`, or every
    reported check when omitted). Idempotent: calling this twice on an
    already-merged PR never re-attempts the merge or raises, and
    completion (event + audit) is recorded exactly once per (repo, PR,
    head sha) regardless of which call - or which of the two paths that
    can observe a completed merge - gets there first.
    """
    run = run_fn or subprocess.run
    # Canonicalized ONCE here (consistent with #17's own GitHubClient._repo)
    # and used for every query/key/event/audit/CLI call below - #37 round 4
    # finding: _review_passed compared repo case-insensitively, but the
    # idempotency key and recorded event/audit still used the caller's raw
    # casing, so calling with "org/repo" then "Org/Repo" for the same
    # PR/head (both authorized by the same PASS) produced two distinct
    # run_sync_once keys and duplicated MERGE_COMPLETED + success audits.
    repo = repo.lower()

    def _fail(reason: str, *, result: str = "skipped") -> MergeOutcome:
        outcome = MergeOutcome(merged=False, reason=reason)
        audit_record(store, action="auto_merge", origin="merge_policy", result=result,
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": reason})
        return outcome

    def _reconcile_merged(merge_commit_oid) -> MergeOutcome:
        """Records MERGE_COMPLETED + the success audit entry atomically,
        exactly once per (repo, pr_number, expected_head_sha) - called
        from both "GitHub already reports this merged" and "we just
        merged it and confirmed", so a crash on either path before this
        point is safely retried into the same outcome."""
        key = f"merge_policy:{repo}:{pr_number}:{expected_head_sha}"

        def apply(connection):
            emit_in_transaction(
                connection, EventType.MERGE_COMPLETED,
                {"repo": repo, "pr_number": pr_number, "task_id": task.id,
                 "head_sha": expected_head_sha, "merge_commit": merge_commit_oid},
                correlation_id=task.correlation_id, project_id=task.project_id,
            )
            audit_record_in_transaction(
                connection, action="auto_merge", origin="merge_policy", result="success",
                correlation_id=task.correlation_id, project_id=task.project_id,
                extra={"pr_number": pr_number, "repo": repo, "reason": "merged"},
            )

        newly_recorded = store.run_sync_once(key, "merged", apply)
        return MergeOutcome(merged=True, reason="merged" if newly_recorded else "already_merged",
                             already_merged=not newly_recorded)

    if not _review_passed(store, task, repo, pr_number, expected_head_sha):
        return _fail("review_not_passed")

    try:
        pr = _pr_view(repo, pr_number, run, timeout)
    except _Failure as error:
        return _fail(error.reason, result="failed")

    if pr.get("state") == "MERGED":
        if pr.get("baseRefName") != expected_base:
            # Merged, but into the WRONG target (e.g. main instead of the
            # integration checkpoint) - never claimed as OUR completion.
            return _fail("merged_unexpected_base", result="failed")
        if pr.get("headRefOid") != expected_head_sha:
            # Some other commit ended up merged (a race, or a stale call
            # against a PR that moved on) - never claimed as OUR success.
            return _fail("merged_different_head", result="failed")
        return _reconcile_merged((pr.get("mergeCommit") or {}).get("oid"))

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

    if confirmed.get("baseRefName") != expected_base:
        return _fail("merged_unexpected_base", result="failed")

    if confirmed.get("headRefOid") != expected_head_sha:
        # Between our pre-merge check and this confirmation, a different
        # commit got merged instead (another actor's push+merge raced
        # ours) - --match-head-commit should have refused our OWN merge
        # command in that case, but the confirmation is checked
        # independently rather than trusting that alone.
        return _fail("merged_different_head", result="failed")

    return _reconcile_merged((confirmed.get("mergeCommit") or {}).get("oid"))
