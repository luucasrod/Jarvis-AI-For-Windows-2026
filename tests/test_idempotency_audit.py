"""Cross-cutting idempotency audit (issue #41).

Each module already tests its OWN idempotency in isolation (#17, #18, #19,
#37). This file simulates a crash AT THE BOUNDARY between two modules -
the actual gap #41 exists to find - and asserts the retry of the full
sequence never duplicates the observable side effect (a GitHub Issue, a
Telegram message, or a merge completion record). Fakes are duplicated
locally rather than imported from other test files, matching this
codebase's existing convention (see test_task_materialize.py's own note).
"""
import json
import subprocess

import pytest

import orchestrator.paperclip_ops as paperclip_ops
from orchestrator.audit import query_audit
from orchestrator.config import OrchestratorConfig
from orchestrator.decisions import create_pending_decision
from orchestrator.events import EventType, emit, query_events
from orchestrator.github_client import GitHubClient
from orchestrator.merge_policy import MergeOutcome, try_auto_merge
from orchestrator.models import Task
from orchestrator.paperclip_ops import create_task_idempotent
from orchestrator.persistence import Store
from orchestrator.planner import PlanResult
from orchestrator.project_resolver import ProjectContext
from orchestrator.task_queue import materialize_plan

_TELEGRAM_CONFIG = OrchestratorConfig(
    telegram_bot_token="fake-token", telegram_control_chat_id="111", telegram_report_chat_id="222",
)


# --- Scenario 1: crash after creating the GitHub Issue, before the ---------
# --- Paperclip task is registered -----------------------------------------

class _FakeGitHub:
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


class _FakePaperclipTransport:
    def __init__(self):
        self.tasks = []

    def list_company_tasks(self, company_id, query=None, base_url=None, timeout=None):
        if query is None:
            return list(self.tasks), None
        return [t for t in self.tasks if query in (t.get('description') or '')], None

    def create_task(self, company_id, title, description, assignee_agent_id=None, *, base_url=None, timeout=None):
        task = {'id': f'pc-{len(self.tasks) + 1}', 'title': title, 'description': description}
        self.tasks.append(task)
        return task, None


PROJECT = ProjectContext(
    canonical_id="hub", root="/repo", repository="owner/repo",
    task_source="GitHub Issues (`gh issue list` in this repo) - the real work queue",
)


def test_github_issue_then_paperclip_crash_recovers_without_duplicating(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    github = _FakeGitHub()
    gh_client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    task = Task(title="Do the thing", objective="obj", project_id="hub")

    numbers = materialize_plan(PlanResult(tasks=[task]), PROJECT, store, client=gh_client)
    assert numbers == [1]
    # Simulated crash HERE: process restarts before Paperclip registration
    # ever ran for this task.

    transport = _FakePaperclipTransport()
    monkeypatch.setattr(paperclip_ops.client, 'list_company_tasks', transport.list_company_tasks)
    monkeypatch.setattr(paperclip_ops.client, 'create_task', transport.create_task)

    # Recovery pass: re-run the FULL sequence, not just the missing half -
    # a real restart re-materializes from the same persisted plan/task.
    numbers_retry = materialize_plan(PlanResult(tasks=[task]), PROJECT, store, client=gh_client)
    result = create_task_idempotent(
        "acme", task.title, task.objective, task.correlation_id, store=store, config=OrchestratorConfig(),
    )

    assert numbers_retry == [1]
    assert len(github.posts) == 1  # no second Issue from the retried materialize_plan
    assert result["available"] is True
    assert len(transport.tasks) == 1

    # A SECOND retry (another crash right after this one) must not
    # register a second Paperclip task either.
    result2 = create_task_idempotent(
        "acme", task.title, task.objective, task.correlation_id, store=store, config=OrchestratorConfig(),
    )
    assert result2["available"] is True
    assert len(transport.tasks) == 1
    store.close()


# --- Scenario 2: crash right after a Telegram send is CONFIRMED, before ---
# --- the caller records that it happened -----------------------------------

def test_telegram_decision_send_is_not_duplicated_on_retry(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    sent = []

    def fake_send(message, config=None, post_fn=None, *, store=None):
        sent.append(message)
        return True, None

    monkeypatch.setattr("orchestrator.decisions.send_control_message", fake_send)

    first = create_pending_decision(task, "mesma decisao", store)
    # Simulated crash HERE: caller never got to record "delivered", so it
    # retries the identical call believing the send may not have happened.
    second = create_pending_decision(task, "mesma decisao", store)

    assert first == (True, None)
    assert second == (True, None)
    assert len(sent) == 1  # NOT sent twice - the real #41 gap this fixes
    store.close()


def test_telegram_decision_send_retries_normally_after_a_real_failure(tmp_path, monkeypatch):
    # A previously FAILED attempt (offline, timeout, ...) must still be
    # retried for real - only a CONFIRMED send is ever skipped.
    store = Store(tmp_path / "state.db")
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    attempts = []

    def flaky_send(message, config=None, post_fn=None, *, store=None):
        attempts.append(message)
        if len(attempts) == 1:
            return False, "offline"
        return True, None

    monkeypatch.setattr("orchestrator.decisions.send_control_message", flaky_send)

    first = create_pending_decision(task, "mensagem", store)
    second = create_pending_decision(task, "mensagem", store)

    assert first == (False, "offline")
    assert second == (True, None)
    assert len(attempts) == 2  # real retry after a genuine failure
    store.close()


# --- Scenario 3: crash right after a merge completes, before a caller ------
# --- applies the resulting state -------------------------------------------

_HEAD = "a" * 40
_REPO, _BASE = "org/repo", "integration/orchestration"


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _view_json(*, state="MERGED", head=_HEAD, base=_BASE, merge_commit="deadbeef"):
    return json.dumps({
        "state": state, "isDraft": False, "baseRefName": base, "headRefOid": head,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "pytest", "conclusion": "SUCCESS", "status": "COMPLETED"}],
        "mergeCommit": {"oid": merge_commit},
    })


def test_merge_completion_is_not_duplicated_after_simulated_crash_retry(tmp_path):
    store = Store(tmp_path / "state.db")
    task = Task(title="Fix bug", objective="obj", project_id="hub")
    emit(store, EventType.REVIEW_PASSED,
         {"task_id": task.id, "head_sha": _HEAD, "repo": _REPO, "pr_number": 42},
         correlation_id=task.correlation_id)

    def run_fn(args, **kwargs):
        return _Result(stdout=_view_json())

    first = try_auto_merge(task, _REPO, 42, store, expected_head_sha=_HEAD, expected_base=_BASE,
                           required_checks=["pytest"], run_fn=run_fn)
    # Simulated crash HERE: nothing yet consumes this completion to update
    # the Task's own state (tracked separately - see follow-up), but the
    # completion record itself must still never duplicate on retry.
    second = try_auto_merge(task, _REPO, 42, store, expected_head_sha=_HEAD, expected_base=_BASE,
                            required_checks=["pytest"], run_fn=run_fn)

    assert first == MergeOutcome(merged=True, reason="merged", already_merged=False)
    assert second == MergeOutcome(merged=True, reason="already_merged", already_merged=True)
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
    success_audits = [e for e in query_audit(store) if e["result"] == "success"]
    assert len(success_audits) == 1
    store.close()
