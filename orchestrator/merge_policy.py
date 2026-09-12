"""Automatic PR merge policy (issue #37).

Merges a PR automatically only when ALL of the following hold: the task's
last recorded review verdict (#29's REVIEW_PASSED/REVIEW_FAILED events) is
PASS, the PR's required CI checks are green, and the PR has no merge
conflict with its base. A failed/blocked check is a normal "don't merge
yet" outcome, never an automatic escalation to Lucas - #29 already owns
that escalation policy, and this module's OUT OF SCOPE explicitly excludes
resolving merge conflicts (that stays the implementing agent's normal job).

Every decision (merged, skipped, or failed) is recorded via #20's audit
log, per the issue's own risk-mitigation note (RISK: Alto).

All GitHub calls go through `gh` CLI via subprocess with argument arrays,
same pattern as #17's github_client.py - no shell involved, still fully
injectable for tests. This module never calls `gh` with a real network
connection during its own test suite.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from orchestrator.audit import record as audit_record
from orchestrator.events import EventType, emit, query_events
from orchestrator.models import Task
from orchestrator.persistence import Store


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
    args = [
        "gh", "pr", "view", str(pr_number), "--repo", repo,
        "--json", "merged,mergeable,statusCheckRollup",
    ]
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
    if not isinstance(rollup, list):
        return False
    by_name: dict[str, str] = {}
    for entry in rollup:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            return False
        by_name[entry["name"]] = entry.get("conclusion")
    names = required_checks if required_checks is not None else list(by_name)
    if not names:
        # No checks configured/reported at all is not proof of a green
        # build - never merge on the absence of evidence.
        return False
    return all(by_name.get(name) == "SUCCESS" for name in names)


def _merge_pr(repo: str, pr_number: int, run_fn: Callable, timeout: float) -> None:
    args = ["gh", "pr", "merge", str(pr_number), "--repo", repo, "--merge"]
    try:
        result = run_fn(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise _Failure("gh_not_installed") from None
    except subprocess.TimeoutExpired:
        raise _Failure("timeout") from None
    except OSError:
        raise _Failure("process_unavailable") from None
    if result.returncode != 0:
        raise _Failure("merge_command_failed")


def try_auto_merge(
    task: Task,
    repo: str,
    pr_number: int,
    store: Store,
    *,
    required_checks: list[str] | None = None,
    run_fn: Callable | None = None,
    timeout: float = 30,
) -> MergeOutcome:
    """Merges `pr_number` into its base if review passed, CI is green
    (per `required_checks`, or every reported check when omitted) and
    the PR has no merge conflict. Idempotent: calling this twice on an
    already-merged PR never re-attempts the merge or raises.
    """
    run = run_fn or subprocess.run

    if not _review_passed(store, task):
        outcome = MergeOutcome(merged=False, reason="review_not_passed")
        audit_record(store, action="auto_merge", origin="merge_policy", result="skipped",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    try:
        pr = _pr_view(repo, pr_number, run, timeout)
    except _Failure as error:
        outcome = MergeOutcome(merged=False, reason=error.reason)
        audit_record(store, action="auto_merge", origin="merge_policy", result="failed",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    if pr.get("merged") is True:
        outcome = MergeOutcome(merged=True, reason="already_merged", already_merged=True)
        audit_record(store, action="auto_merge", origin="merge_policy", result="skipped",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    if pr.get("mergeable") != "MERGEABLE":
        outcome = MergeOutcome(merged=False, reason="merge_conflict")
        audit_record(store, action="auto_merge", origin="merge_policy", result="skipped",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    if not _checks_green(pr.get("statusCheckRollup"), required_checks):
        outcome = MergeOutcome(merged=False, reason="ci_not_green")
        audit_record(store, action="auto_merge", origin="merge_policy", result="skipped",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    try:
        _merge_pr(repo, pr_number, run, timeout)
    except _Failure as error:
        outcome = MergeOutcome(merged=False, reason=error.reason)
        audit_record(store, action="auto_merge", origin="merge_policy", result="failed",
                     correlation_id=task.correlation_id, project_id=task.project_id,
                     extra={"pr_number": pr_number, "repo": repo, "reason": outcome.reason})
        return outcome

    outcome = MergeOutcome(merged=True, reason="merged")
    emit(store, EventType.MERGE_COMPLETED, {"repo": repo, "pr_number": pr_number, "task_id": task.id},
         correlation_id=task.correlation_id, project_id=task.project_id)
    audit_record(store, action="auto_merge", origin="merge_policy", result="success",
                 correlation_id=task.correlation_id,
                 extra={"pr_number": pr_number, "repo": repo})
    return outcome
