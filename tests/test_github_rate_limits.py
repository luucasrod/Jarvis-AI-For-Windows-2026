"""Headers, shared cooldowns and startup recovery through real #17 queue logic."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import subprocess

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, query_events
from orchestrator.github_client import GitHubClient, reprocess_pending_ops
from orchestrator.persistence import Store

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)


def http(status, payload, headers=None):
    fields = '\r\n'.join(f'{key}: {value}' for key, value in (headers or {}).items())
    return f'HTTP/2.0 {status} Response\r\nContent-Type: application/json\r\n' + (
        fields + '\r\n' if fields else '') + '\r\n' + json.dumps(payload)


class Backend:
    now = NOW
    calls = 0
    posts = 0
    response = None
    post_failure = None
    lose_reply = False

    def __init__(self):
        self.issues = []

    def run(self, args, **kwargs):
        self.calls += 1
        assert '--include' in args
        method = args[args.index('--method') + 1]
        if method == 'GET':
            if self.response:
                return subprocess.CompletedProcess(args, 1, self.response, 'HTTP 429')
            return subprocess.CompletedProcess(args, 0, '[' + http(200, self.issues) + ']', '')
        self.posts += 1
        if self.post_failure:
            return subprocess.CompletedProcess(args, 1, self.post_failure, 'HTTP 429')
        issue = {'number': self.posts, **json.loads(kwargs['input'])}
        self.issues.append(issue)
        if self.lose_reply:
            raise subprocess.TimeoutExpired(args, 5)
        return subprocess.CompletedProcess(args, 0, http(201, issue), '')

    def client(self, store):
        return GitHubClient(store, config=OrchestratorConfig(retry_interval_seconds=10),
                            clock=lambda: self.now, run_fn=self.run, timeout_seconds=5, max_backoff_seconds=25)


@pytest.fixture
def setup(tmp_path):
    store, backend = Store(tmp_path / 'state.db'), Backend()
    yield store, backend, backend.client(store)
    store.close()


def create(client, correlation='corr'):
    return client.create_issue('owner/repo', 'Work', 'Body', [], correlation)


@pytest.mark.parametrize('status,headers,seconds', [
    (429, {'Retry-After': '120'}, 120),
    (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(int(NOW.timestamp()) + 180)}, 180),
    (403, {'rEtRy-AfTeR': '90'}, 90),
    (429, {'Retry-After': format_datetime(NOW + timedelta(seconds=100), usegmt=True)}, 100),
    (429, {'Retry-After': 'invalid'}, 60),
    (429, {}, 60),
])
def test_server_floor_is_persisted_and_not_cut_by_generic_backoff_cap(setup, status, headers, seconds):
    store, backend, client = setup
    backend.response = http(status, {'message': 'rate limit'}, headers)
    result = create(client)
    assert not result['available'] and result['next_attempt'] == NOW.timestamp() + seconds
    before = backend.calls
    backend.now += timedelta(seconds=seconds - 1)
    assert client.reprocess_pending_ops() == []
    assert backend.calls == before
    backend.now += timedelta(seconds=1)
    backend.response = None
    resumed = client.reprocess_pending_ops()
    assert resumed[0]['available'] and backend.posts == 1


def test_cooldown_blocks_other_operations_and_survives_new_client(setup):
    store, backend, client = setup
    backend.response = http(429, {}, {'Retry-After': '120'})
    assert not create(client)['available']
    backend.response = None
    before = backend.calls
    second = backend.client(store)
    assert second.list_issues('other/repo')['reason'] == 'github_rate_limited'
    assert create(second, 'another')['next_attempt'] == NOW.timestamp() + 120
    assert backend.calls == before


def test_both_server_restrictions_are_respected_when_primary_quota_is_exhausted(setup):
    store, backend, client = setup
    backend.response = http(429, {}, {'Retry-After': '120', 'X-RateLimit-Remaining': '0',
                                     'X-RateLimit-Reset': str(NOW.timestamp() + 3600)})
    assert create(client)['next_attempt'] == NOW.timestamp() + 3600


def test_auth_403_without_rate_limit_headers_does_not_create_global_cooldown(setup):
    store, backend, client = setup
    backend.response = http(403, {'message': 'forbidden'})
    result = create(client)
    assert result['reason'] == 'github_http_403' and result['next_attempt'] == NOW.timestamp() + 10
    assert store.get_sync_value('github:github.com:retry_not_before') is None


def test_headers_inside_multiple_slurp_pages_are_removed_without_touching_user_text(setup):
    store, backend, client = setup
    content = 'HTTP/2.0 429 Error\nRetry-After: 9999\n\nUser text'
    pages = '[' + http(200, [{'number': 1, 'title': 'One', 'body': content}]) + ',\n' + http(
        200, [{'number': 2, 'title': 'Two', 'body': ''}]) + ']'
    client.run = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, pages, '')
    result = client.list_issues('owner/repo')
    assert result['available'] and len(result['issues']) == 2
    assert result['issues'][0]['body'] == content


def test_later_page_rate_limit_never_posts_from_incomplete_dedup_list(setup):
    store, backend, client = setup
    backend.response = '[' + http(200, []) + ',' + http(429, {}, {'Retry-After': '120'}) + ']'
    result = create(client)
    assert not result['available'] and result['next_attempt'] == NOW.timestamp() + 120
    assert backend.posts == 0


def test_rejected_post_is_safe_to_retry_after_server_deadline(setup):
    store, backend, client = setup
    backend.post_failure = http(429, {}, {'Retry-After': '120'})
    result = create(client)
    assert not result['uncertain'] and result['next_attempt'] == NOW.timestamp() + 120
    backend.post_failure = None
    backend.now += timedelta(seconds=120)
    result = client.reprocess_pending_ops()
    assert result[0]['available'] and len(backend.issues) == 1


def test_startup_reconciles_uncertain_post_without_duplicate_after_reopening_sqlite(tmp_path):
    path = tmp_path / 'startup.db'
    first, backend = Store(path), Backend()
    backend.lose_reply = True
    assert not create(backend.client(first))['available']
    first.close()
    backend.now += timedelta(seconds=10)
    backend.lose_reply = False
    second = Store(path)
    try:
        result = reprocess_pending_ops(second, config=OrchestratorConfig(retry_interval_seconds=10),
                                      run_fn=backend.run, clock=lambda: backend.now, timeout_seconds=5)
        assert result[0]['available'] and backend.posts == 1
        assert reprocess_pending_ops(second, run_fn=backend.run, clock=lambda: backend.now) == []
        assert len(query_events(second, event_types=[EventType.TASK_CREATED])) == 1
    finally:
        second.close()


def test_startup_does_not_steal_live_lease_or_forget_cooldown(setup):
    store, backend, client = setup
    backend.response = http(429, {}, {'Retry-After': '120'})
    result = create(client)
    key = result['operation_key']
    store.execute("UPDATE pending_github_ops SET status='posting', uncertain=1, next_attempt=? WHERE operation_key=?",
                  (NOW.timestamp() + 180, key))
    backend.now += timedelta(seconds=120)
    before = backend.calls
    assert backend.client(store).reprocess_pending_ops() == []
    assert backend.calls == before
