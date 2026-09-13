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
import sqlite3
import subprocess
import threading

import pytest

import orchestrator.paperclip_ops as paperclip_ops
from orchestrator.audit import query_audit
from orchestrator.config import OrchestratorConfig
from orchestrator.decisions import _decision_ref, create_pending_decision
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


def test_paperclip_then_github_crash_recovers_without_duplicating(tmp_path, monkeypatch):
    # The mirror direction Review Task #133 round 2 asked for: Paperclip
    # registration happens FIRST, then a crash before the GitHub Issue is
    # ever created for the same task.
    store = Store(tmp_path / "state.db")
    task = Task(title="Do the other thing", objective="obj", project_id="hub")

    transport = _FakePaperclipTransport()
    monkeypatch.setattr(paperclip_ops.client, 'list_company_tasks', transport.list_company_tasks)
    monkeypatch.setattr(paperclip_ops.client, 'create_task', transport.create_task)

    result = create_task_idempotent(
        "acme", task.title, task.objective, task.correlation_id, store=store, config=OrchestratorConfig(),
    )
    assert result["available"] is True
    assert len(transport.tasks) == 1
    # Simulated crash HERE: process restarts before the GitHub Issue is
    # ever created for this task.

    github = _FakeGitHub()
    gh_client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    # Recovery pass: re-run the FULL sequence again.
    result_retry = create_task_idempotent(
        "acme", task.title, task.objective, task.correlation_id, store=store, config=OrchestratorConfig(),
    )
    numbers = materialize_plan(PlanResult(tasks=[task]), PROJECT, store, client=gh_client)

    assert result_retry["available"] is True
    assert len(transport.tasks) == 1  # no second Paperclip task
    assert numbers == [1]
    assert len(github.posts) == 1

    # A SECOND retry must not create a second Issue either.
    numbers_retry = materialize_plan(PlanResult(tasks=[task]), PROJECT, store, client=gh_client)
    assert numbers_retry == [1]
    assert len(github.posts) == 1
    store.close()


# --- Scenario 2: crash right after a Telegram send is CONFIRMED, before ---
# --- the caller records that it happened -----------------------------------

def test_pre_upgrade_confirmed_delivery_is_not_resent(tmp_path, monkeypatch):
    # Round-3 finding: a decision confirmed under the PREVIOUS design (a
    # plain idempotency_keys row, kind "decision_telegram_send", written
    # by the pre-#136 create_pending_decision) must be recognized and
    # migrated, not silently resent just because the storage format
    # changed underneath it on upgrade.
    db_path = tmp_path / "state.db"
    store = Store(db_path)
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    message = "mesma decisao"
    correlation_id = f"{task.correlation_id}:{_decision_ref(task, message)}"
    store.save_decision(correlation_id=correlation_id, task_id=task.id, message=message)
    store.record_idempotency_key(correlation_id, "decision_telegram_send")
    store.close()

    sent = []

    def fake_send(message, config=None, post_fn=None, *, store=None):
        sent.append(message)
        return True, None

    monkeypatch.setattr("orchestrator.decisions.send_control_message", fake_send)

    reopened = Store(db_path)
    result = create_pending_decision(task, message, reopened, correlation_id=correlation_id)

    assert result == (True, None)
    assert sent == []  # never resent - the pre-upgrade confirmation stands
    reopened.close()


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


def test_telegram_decision_send_retries_normally_after_a_definite_failure(tmp_path, monkeypatch):
    # A PROVABLY-not-sent failure (bad token, bad config) must still be
    # retried for real once fixed - only a CONFIRMED send, or an UNCERTAIN
    # one, is ever skipped.
    store = Store(tmp_path / "state.db")
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    attempts = []

    def flaky_send(message, config=None, post_fn=None, *, store=None):
        attempts.append(message)
        if len(attempts) == 1:
            return False, "token invalido"
        return True, None

    monkeypatch.setattr("orchestrator.decisions.send_control_message", flaky_send)

    first = create_pending_decision(task, "mensagem", store)
    second = create_pending_decision(task, "mensagem", store)

    assert first == (False, "token invalido")
    assert second == (True, None)
    assert len(attempts) == 2  # real retry after a provably-not-sent failure
    store.close()


def test_telegram_uncertain_failure_is_never_auto_resent(tmp_path, monkeypatch):
    # Round-2 finding #2: a timeout does NOT prove the message was never
    # delivered - it must be treated as uncertain, not silently retried.
    store = Store(tmp_path / "state.db")
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    attempts = []

    def flaky_send(message, config=None, post_fn=None, *, store=None):
        attempts.append(message)
        return False, "timeout"

    monkeypatch.setattr("orchestrator.decisions.send_control_message", flaky_send)

    first = create_pending_decision(task, "mensagem", store)
    second = create_pending_decision(task, "mensagem", store)

    assert first == (False, "timeout")
    assert second[0] is False and "incerta" in second[1]
    assert len(attempts) == 1  # never auto-resent after an uncertain outcome
    store.close()


def test_telegram_crash_between_confirmed_send_and_local_commit_is_never_resent(tmp_path, monkeypatch):
    # Round-2 finding #1's exact repro: inject a failure into the record-
    # of-success step itself, AFTER send_control_message already returned
    # True, close the Store (simulating the crash), reopen the SAME db
    # file as a fresh process would, and retry - must never resend.
    db_path = tmp_path / "state.db"
    store = Store(db_path)
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    sent = []

    def fake_send(message, config=None, post_fn=None, *, store=None):
        sent.append(message)
        return True, None

    monkeypatch.setattr("orchestrator.decisions.send_control_message", fake_send)

    original_run_in_transaction = store.run_in_transaction
    calls = {"n": 0}

    def crashing_run_in_transaction(operation):
        calls["n"] += 1
        if calls["n"] == 2:  # 1st call is the claim; 2nd is the confirm
            raise sqlite3.OperationalError("simulated crash before commit")
        return original_run_in_transaction(operation)

    monkeypatch.setattr(store, "run_in_transaction", crashing_run_in_transaction)

    with pytest.raises(sqlite3.OperationalError):
        create_pending_decision(task, "mesma decisao", store)
    store.close()

    reopened = Store(db_path)
    result = create_pending_decision(task, "mesma decisao", reopened)

    assert len(sent) == 1  # never resent - the real gap this closes
    assert result[0] is False and "incerta" in result[1]
    reopened.close()


def test_telegram_concurrent_deliveries_do_not_both_send(tmp_path, monkeypatch):
    # Round-2 finding #3: two concurrent connections to the same db must
    # not both pass the check and both send.
    db_path = tmp_path / "state.db"
    task = Task(title="Precisa decidir", objective="obj", project_id="hub")
    sent = []
    send_started = threading.Event()
    release_send = threading.Event()

    def fake_send(message, config=None, post_fn=None, *, store=None):
        sent.append(message)
        send_started.set()
        release_send.wait(timeout=5)
        return True, None

    monkeypatch.setattr("orchestrator.decisions.send_control_message", fake_send)

    store_a = Store(db_path)
    store_b = Store(db_path)
    results = {}

    def first():
        results["a"] = create_pending_decision(task, "mesma decisao", store_a)

    def second():
        assert send_started.wait(timeout=5)
        results["b"] = create_pending_decision(task, "mesma decisao", store_b)
        release_send.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(sent) == 1
    assert results["b"][0] is False and "incerta" in results["b"][1]
    store_a.close()
    store_b.close()


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


def test_merge_completion_registers_exactly_once_after_confirmation_crash(tmp_path):
    # The real boundary Review Task #133 round 2 asked for: a SINGLE remote
    # merge happens for real (gh pr merge called exactly once), the process
    # then fails to CONFIRM it (simulating a crash right after the remote
    # merge succeeded, before try_auto_merge ever reaches _reconcile_merged),
    # and a recovery pass - which must NOT attempt another remote merge -
    # completes the local registration exactly once.
    store = Store(tmp_path / "state.db")
    task = Task(title="Fix bug", objective="obj", project_id="hub")
    emit(store, EventType.REVIEW_PASSED,
         {"task_id": task.id, "head_sha": _HEAD, "repo": _REPO, "pr_number": 42},
         correlation_id=task.correlation_id)

    calls = {"merge": 0, "view": 0}

    def crashing_run_fn(args, **kwargs):
        if args[1:3] == ["pr", "merge"]:
            calls["merge"] += 1
            return _Result()
        calls["view"] += 1
        if calls["view"] == 1:
            return _Result(stdout=_view_json(state="OPEN"))
        # Confirmation view right after the real merge succeeded - the
        # process crashes here, before _reconcile_merged ever runs.
        return _Result(returncode=1, stderr="transport failure")

    first = try_auto_merge(task, _REPO, 42, store, expected_head_sha=_HEAD, expected_base=_BASE,
                           required_checks=["pytest"], run_fn=crashing_run_fn)
    assert first.merged is False
    assert calls["merge"] == 1  # exactly one real remote merge happened

    def recovered_run_fn(args, **kwargs):
        assert args[1:3] != ["pr", "merge"]  # recovery must never merge again
        return _Result(stdout=_view_json(state="MERGED"))

    second = try_auto_merge(task, _REPO, 42, store, expected_head_sha=_HEAD, expected_base=_BASE,
                            required_checks=["pytest"], run_fn=recovered_run_fn)

    assert second == MergeOutcome(merged=True, reason="merged", already_merged=False)
    assert len(query_events(store, event_types=[EventType.MERGE_COMPLETED])) == 1
    success_audits = [e for e in query_audit(store) if e["result"] == "success"]
    assert len(success_audits) == 1
    store.close()
