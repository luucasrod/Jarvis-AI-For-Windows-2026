"""GitHub durable queue tests using a fake gh subprocess, no network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, query_events
from orchestrator.github_client import GitHubClient
from orchestrator.persistence import Store


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class GitHub:
    def __init__(self):
        self.issues, self.calls, self.posts, self.patches = [], [], [], []
        self.read_error = self.post_error = self.patch_error = None
        self.lose_reply = False
        self.pages = None
    def run(self, args, **kwargs):
        method = args[args.index('--method') + 1]
        self.calls.append(args)
        assert kwargs['timeout'] == 5
        assert kwargs['env']['GH_PROMPT_DISABLED'] == '1'
        assert kwargs.get('shell', False) is False
        if method == 'GET':
            if self.read_error:
                return subprocess.CompletedProcess(args, 1, '', self.read_error)
            return subprocess.CompletedProcess(args, 0, json.dumps(self.pages if self.pages is not None else [self.issues]), '')
        if method == 'PATCH':
            endpoint = args[args.index('--method') + 2]
            number = int(endpoint.rsplit('/', 1)[-1])
            body = json.loads(kwargs['input'])
            self.patches.append(body)
            if self.patch_error:
                return subprocess.CompletedProcess(args, 1, '', self.patch_error)
            for issue in self.issues:
                if issue['number'] == number:
                    issue['body'] = body['body']
                    return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')
            return subprocess.CompletedProcess(args, 1, '', 'not found')
        body = json.loads(kwargs['input'])
        self.posts.append(body)
        if self.post_error:
            return subprocess.CompletedProcess(args, 1, '', self.post_error)
        issue = {'number': len(self.posts), **body, 'state': 'open'}
        self.issues.append(issue)
        if self.lose_reply:
            raise subprocess.TimeoutExpired(args, 5)
        return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')


@pytest.fixture
def setup(tmp_path):
    store, clock, github = Store(tmp_path / 'state.db'), Clock(), GitHub()
    client = GitHubClient(store, config=OrchestratorConfig(retry_interval_seconds=10),
                          run_fn=github.run, clock=clock, timeout_seconds=5, max_backoff_seconds=25)
    yield store, clock, github, client
    store.close()


def create(client, **changes):
    args = dict(repo='Owner/Repo', title='Task', body='Description', labels=['wave:1'], correlation_id='corr')
    args.update(changes)
    return client.create_issue(**args)


def test_simple_create_duplicate_key_and_event_once(setup):
    store, clock, github, client = setup
    first, second = create(client), create(client)
    assert first['available'] and second['available']
    assert first['number'] == second['number'] == 1
    assert second['cached']
    assert len(github.posts) == 1
    assert first['url'] == 'https://github.com/owner/repo/issues/1'
    assert store.has_idempotency_key('corr', 'github_issue:owner/repo')
    events = query_events(store, event_types=[EventType.TASK_CREATED], correlation_id='corr')
    assert len(events) == 1 and events[0]['payload']['issue_number'] == 1


def test_title_dedup_including_closed_issue_and_ignoring_pr(setup):
    store, clock, github, client = setup
    github.issues = [{'number': 7, 'title': ' TASK ', 'body': None, 'state': 'closed'},
                     {'number': 9, 'title': 'Task', 'pull_request': {}}]
    result = create(client)
    assert result['available'] and result['number'] == 7
    assert github.posts == []
    assert query_events(store) == []  # reusing old work is not a new creation


def test_ambiguous_title_does_not_create(setup):
    store, clock, github, client = setup
    github.issues = [{'number': number, 'title': 'Task', 'body': ''} for number in (1, 2)]
    result = create(client)
    assert result['reason'] == 'ambiguous_duplicate' and not result['pending']
    assert github.posts == []


def test_read_offline_backoff_cap_and_eventual_success(setup):
    store, clock, github, client = setup
    github.read_error = 'error connecting to api.github.com'
    first = create(client)
    assert first['pending'] and not first['uncertain']
    assert first['next_attempt'] == clock.now.timestamp() + 10
    assert client.retry_pending() == []
    clock.advance(10)
    second = client.retry_pending()[0]
    assert second['next_attempt'] == clock.now.timestamp() + 20
    clock.advance(20)
    third = client.retry_pending()[0]
    assert third['next_attempt'] == clock.now.timestamp() + 25
    clock.advance(25)
    github.read_error = None
    assert client.retry_pending()[0]['available']
    assert len(github.posts) == 1


def test_definitely_unsent_post_is_retried(setup):
    store, clock, github, client = setup
    github.post_error = 'dial tcp: connection refused'
    result = create(client)
    assert result['pending'] and not result['uncertain']
    clock.advance(10)
    github.post_error = None
    assert client.retry_pending()[0]['available']
    assert len(github.posts) == 2 and len(github.issues) == 1


def test_lost_post_reply_reconciles_once(setup):
    store, clock, github, client = setup
    github.lose_reply = True
    result = create(client)
    assert result['reason'] == 'timeout' and result['uncertain']
    assert not store.has_idempotency_key('corr', 'github_issue:owner/repo')
    clock.advance(10)
    result = client.retry_pending()[0]
    assert result['available'] and result['number'] == 1
    assert len(github.posts) == 1
    assert len(query_events(store)) == 1


def test_unknown_post_with_no_match_does_not_blindly_retry(setup):
    store, clock, github, client = setup
    github.post_error = 'connection reset by peer'
    assert create(client)['uncertain']
    clock.advance(10)
    github.post_error = None
    result = client.retry_pending()[0]
    assert result['reason'] == 'creation_unconfirmed' and result['uncertain']
    assert len(github.posts) == 1


def test_queue_and_completed_result_survive_restart(tmp_path):
    path, clock, github = tmp_path / 'state.db', Clock(), GitHub()
    github.read_error = 'network down'
    first = Store(path)
    client = GitHubClient(first, run_fn=github.run, clock=clock, timeout_seconds=5,
                          config=OrchestratorConfig(retry_interval_seconds=10))
    assert create(client)['pending']
    first.close()
    second = Store(path)
    try:
        client = GitHubClient(second, run_fn=github.run, clock=clock, timeout_seconds=5)
        assert client.retry_pending() == []
        clock.advance(10)
        github.read_error = None
        assert client.retry_pending()[0]['available']
    finally:
        second.close()
    third = Store(path)
    try:
        client = GitHubClient(third, run_fn=github.run, clock=clock, timeout_seconds=5)
        github.read_error = 'network down'
        assert create(client)['available']
        assert len(github.posts) == 1
    finally:
        third.close()


def test_concurrent_connections_create_one_issue(tmp_path):
    path, github, clock, barrier = tmp_path / 'state.db', GitHub(), Clock(), Barrier(4)
    stores = [Store(path) for _ in range(4)]
    clients = [GitHubClient(store, run_fn=github.run, clock=clock, timeout_seconds=5) for store in stores]
    def execute(client):
        barrier.wait(timeout=5)
        return create(client)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(execute, clients))
        assert any(result['available'] for result in results)
        assert len(github.posts) == 1
        assert len(query_events(stores[0])) == 1
    finally:
        for store in stores:
            store.close()


def test_completion_rolls_back_result_key_and_event_then_reconciles(setup, monkeypatch):
    import orchestrator.github_client as module
    store, clock, github, client = setup
    original = module.emit_in_transaction
    def fail_after_emit(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError('simulated commit interruption')
    monkeypatch.setattr(module, 'emit_in_transaction', fail_after_emit)
    assert create(client)['reason'] == 'local_persistence_error'
    assert not store.has_idempotency_key('corr', 'github_issue:owner/repo')
    assert query_events(store) == []
    monkeypatch.setattr(module, 'emit_in_transaction', original)
    clock.advance(41)  # expired owner lease; durable posting flag means reconcile only
    result = client.retry_pending()[0]
    assert result['available'] and result['number'] == 1
    assert len(github.posts) == 1 and len(query_events(store)) == 1


def test_expired_read_claim_can_retry_but_stale_owner_cannot_send(setup, monkeypatch):
    store, clock, github, client = setup
    original = client.list_issues
    inner_result = []
    entered = False
    def takeover(*args, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            clock.advance(41)
            inner_result.extend(client.retry_pending())
        return original(*args, **kwargs)
    monkeypatch.setattr(client, 'list_issues', takeover)
    assert create(client)['available']
    assert inner_result[0]['available']
    assert len(github.posts) == 1


def test_repo_scope_conflict_and_json_stdin_preserve_literal_content(setup):
    store, clock, github, client = setup
    title = 'Task $(not-a-command) `literal`'
    body = 'Line one\nLine two "quote"'
    assert create(client, title=title, body=body)['available']
    assert github.posts[0]['title'] == title
    assert github.posts[0]['body'].startswith(body + '\n\n')
    assert create(client, title='changed')['reason'] == 'correlation_conflict'
    github.issues = []
    assert create(client, repo='other/repo')['available']
    assert len(github.posts) == 2


def test_paginated_listing_with_state_and_labels(setup):
    store, clock, github, client = setup
    github.pages = [[{'number': 1, 'title': 'First'}], [{'number': 2, 'title': 'Second'}]]
    result = client.list_issues('Owner/Repo', state='closed', labels=['x', 'y z'])
    assert [item['number'] for item in result['issues']] == [1, 2]
    args = github.calls[0]
    assert '--paginate' in args and '--slurp' in args
    endpoint = args[args.index('--method') + 2]
    assert parse_qs(urlparse(endpoint).query)['labels'] == ['x,y z']
    assert parse_qs(urlparse(endpoint).query)['state'] == ['closed']


@pytest.mark.parametrize('pages', [None, {}, [None], [[{'number': True, 'title': 'Bad'}]],
                                 [[{'number': 1, 'title': 'Bad', 'body': []}]]])
def test_malformed_responses_cannot_trigger_post(setup, pages, monkeypatch):
    store, clock, github, client = setup
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(pages), '')
    monkeypatch.setattr(client, 'run', run)
    result = create(client)
    assert result['pending'] and result['reason'] == 'invalid_issue_list'
    assert github.posts == []


@pytest.mark.parametrize('changes', [{'repo': '../repo'}, {'repo': 'owner/repo?x'}, {'title': ''},
                                     {'body': None}, {'labels': 'bug'}, {'correlation_id': ''}])
def test_invalid_create_does_not_call_cli(setup, changes):
    store, clock, github, client = setup
    assert create(client, **changes)['reason'] == 'invalid_create_input'
    assert github.calls == []


def test_process_errors_are_sanitized_and_pending(setup, monkeypatch):
    store, clock, github, client = setup
    def fail(args, **kwargs):
        raise FileNotFoundError('fake-token-in-error')
    monkeypatch.setattr(client, 'run', fail)
    result = create(client)
    assert result['reason'] == 'gh_not_installed'
    assert result['pending'] and 'fake-token' not in str(result)


def test_validation_rejection_is_persisted_without_retry_storm(setup):
    store, clock, github, client = setup
    github.post_error = 'Validation failed (HTTP 422): fake-token'
    result = create(client)
    assert result['reason'] == 'github_http_422'
    assert not result['pending'] and not result['uncertain']
    clock.advance(10000)
    assert client.retry_pending() == []
    assert store.query('SELECT status FROM pending_github_ops')[0][0] == 'failed'


# --- update_issue_body (added for #23's BLOCKS backfill) -------------------

def test_update_issue_body_replaces_body(setup):
    store, clock, github, client = setup
    create(client)
    result = client.update_issue_body('Owner/Repo', 1, 'new body')
    assert result == {'available': True, 'number': 1}
    assert github.issues[0]['body'] == 'new body'
    assert github.patches == [{'body': 'new body'}]


def test_update_issue_body_repeated_call_is_a_no_op_not_a_duplicate(setup):
    store, clock, github, client = setup
    create(client)
    client.update_issue_body('Owner/Repo', 1, 'new body')
    client.update_issue_body('Owner/Repo', 1, 'new body')
    assert len(github.patches) == 2  # both PATCH calls happen...
    assert github.issues[0]['body'] == 'new body'  # ...but the result is identical, not cumulative


@pytest.mark.parametrize('changes', [{'issue_number': 0}, {'issue_number': -1}, {'issue_number': 'x'},
                                     {'issue_number': True}, {'body': None}, {'repo': '../repo'}])
def test_update_issue_body_rejects_invalid_input(setup, changes):
    store, clock, github, client = setup
    kwargs = dict(repo='Owner/Repo', issue_number=1, body='x')
    kwargs.update(changes)
    result = client.update_issue_body(**kwargs)
    assert result == {'available': False, 'reason': 'invalid_update_input'}
    assert github.patches == []


def test_update_issue_body_reports_transport_failure(setup):
    store, clock, github, client = setup
    create(client)
    github.patch_error = 'server error'
    result = client.update_issue_body('Owner/Repo', 1, 'new body')
    assert result['available'] is False
    assert github.issues[0]['body'] != 'new body'


def test_timeout_and_backoff_default_from_config_when_omitted(tmp_path):
    # Issue #44: these used to be hardcoded 30/3600 constants regardless
    # of config - an explicit caller value must still win, but omitting
    # both must read GITHUB_TIMEOUT_SECONDS/GITHUB_MAX_BACKOFF_SECONDS.
    store = Store(tmp_path / 'state.db')
    client = GitHubClient(
        store, config=OrchestratorConfig(
            retry_interval_seconds=10, github_timeout_seconds=17, github_max_backoff_seconds=222,
        ),
    )
    assert client.timeout == 17
    assert client.max_backoff == 222
    store.close()
