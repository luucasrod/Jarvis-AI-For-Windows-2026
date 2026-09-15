"""Restart detection and cooldown across actual #18 operations (#39)."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

import paperclip_client as client
from orchestrator.config import OrchestratorConfig
from orchestrator.paperclip_ops import PaperclipSession
from orchestrator.persistence import Store


class Response:
    status_code = 200
    text = 'json'

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def runtime(monkeypatch):
    class Runtime:
        now = 100.0
        calls = 0
        posts = 0
        error = None
        health = {'status': 'ok', 'serverInfo': {'processStartedAt': '2026-09-12T07:00:00Z'}}
        tasks = []
        lose_post_response = False

        def get(self, url, **kwargs):
            self.calls += 1
            assert kwargs['timeout'] == 2
            if self.error:
                raise self.error
            return Response(self.health if url.endswith('/api/health') else self.tasks)

        def post(self, url, **kwargs):
            self.posts += 1
            self.tasks = [{'id': 'created', 'status': 'backlog', **kwargs['json']}]
            if self.lose_post_response:
                raise requests.exceptions.Timeout('sensitive response omitted')
            return Response(self.tasks[0])

        def session(self, **kwargs):
            return PaperclipSession(config=OrchestratorConfig(
                paperclip_base_url='http://paperclip.invalid',
                paperclip_timeout_seconds=2, retry_interval_seconds=5),
                clock=lambda: self.now, max_backoff_seconds=20, **kwargs)

    fake = Runtime()
    monkeypatch.setattr(client.requests, 'get', fake.get)
    monkeypatch.setattr(client.requests, 'post', fake.post)
    monkeypatch.setattr(client, '_CONFIG_TOKEN', '')
    monkeypatch.delenv('PAPERCLIP_API_TOKEN', raising=False)
    return fake


def test_restart_same_version_new_process_and_identical_reads(runtime):
    session = runtime.session()
    assert session.detect_restart() is False  # initial baseline
    assert session.detect_restart() is False
    runtime.health = {'status': 'ok', 'version': 'unchanged',
                      'serverInfo': {'processStartedAt': '2026-09-12T08:00:00Z'}}
    assert session.detect_restart() is True
    assert session.detect_restart() is False
    assert session.last_runtime_status == {'available': True, 'restarted': False}


def test_same_instant_different_timezone_not_restart(runtime):
    session = runtime.session()
    session.detect_restart()
    runtime.health = {'status': 'ok', 'serverInfo': {'processStartedAt': '2026-09-12T08:00:00+01:00'}}
    assert session.detect_restart() is False


@pytest.mark.parametrize('health', [None, [], {}, {'status': 'bad'},
    {'status': 'ok', 'serverInfo': None},
    {'status': 'ok', 'serverInfo': {'processStartedAt': 123}},
    {'status': 'ok', 'serverInfo': {'processStartedAt': 'invalid'}},
    {'status': 'ok', 'serverInfo': {'processStartedAt': '2026-09-12T07:00:00'}},
    ValueError('invalid json with secret')])
def test_invalid_read_preserves_identity_and_enters_backoff(runtime, health):
    session = runtime.session()
    session.detect_restart()
    runtime.health = health
    assert session.detect_restart() is False
    assert session.last_runtime_status['available'] is False
    assert 'secret' not in str(session.last_runtime_status)
    assert session.detect_restart() is False
    assert session.last_runtime_status['reason'] == 'backoff'
    assert runtime.calls == 2
    runtime.now += 5
    runtime.health = {'status': 'ok', 'serverInfo': {'processStartedAt': '2026-09-12T08:00:00Z'}}
    assert session.detect_restart() is True


def test_outage_recovery_same_process_is_not_restart(runtime):
    session = runtime.session()
    session.detect_restart()
    runtime.error = requests.exceptions.ConnectionError('sensitive url')
    assert not session.detect_restart()
    assert session.last_runtime_status['reason'] == 'offline'
    runtime.error = None
    runtime.now += 5
    assert not session.detect_restart()
    assert session.last_runtime_status['available']


def test_backoff_grows_caps_and_resets_after_recovery(runtime):
    session = runtime.session()
    runtime.error = requests.exceptions.ConnectionError()
    for delay in (5, 10, 20, 20, 20):
        session.detect_restart()
        assert session.last_runtime_status['retry_after_seconds'] == delay
        calls = runtime.calls
        for _ in range(10):
            assert session.get_task_status('company', 'task')['reason'] == 'backoff'
        assert runtime.calls == calls
        runtime.now += delay
    runtime.error = None
    session.detect_restart()
    assert session.last_runtime_status['available']
    runtime.error = requests.exceptions.Timeout()
    session.detect_restart()
    assert session.last_runtime_status['retry_after_seconds'] == 5


def test_concurrent_failed_calls_allow_only_one_probe(runtime):
    session = runtime.session()
    runtime.error = requests.exceptions.ConnectionError()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: session.get_task_status('c', 't'), range(32)))
    assert runtime.calls == 1
    assert sum(result['reason'] == 'offline' for result in results) == 1
    assert sum(result['reason'] == 'backoff' for result in results) == 31


def test_uncertain_post_cooldown_then_reconcile_without_duplicate(runtime, tmp_path):
    session = runtime.session()
    store = Store(tmp_path / 'state.db')
    try:
        runtime.lose_post_response = True
        first = session.create_task_idempotent('c', 'Title', 'Body', 'corr', store=store)
        assert first['uncertain'] and first['reason'] == 'timeout'
        calls = runtime.calls
        retry = session.create_task_idempotent('c', 'Title', 'Body', 'corr', store=store)
        assert retry['reason'] == 'backoff' and retry['uncertain']
        assert runtime.calls == calls and runtime.posts == 1
        runtime.now += 5
        result = session.create_task_idempotent('c', 'Title', 'Body', 'corr', store=store)
        assert result['available'] and result['reconciled']
        assert runtime.posts == 1
    finally:
        store.close()


def test_invalid_status_list_is_also_backed_off(runtime):
    session = runtime.session()
    runtime.tasks = {'wrong': 'shape'}
    assert session.get_task_status('c', 't')['reason'] == 'invalid_task_list'
    assert session.get_task_status('c', 't')['reason'] == 'backoff'
    assert runtime.calls == 1


def test_backoff_is_measured_after_slow_request_completes(runtime, monkeypatch):
    session = runtime.session()
    def slow_get(*args, **kwargs):
        runtime.now += 6
        raise requests.exceptions.Timeout()
    monkeypatch.setattr(client.requests, 'get', slow_get)
    session.detect_restart()
    runtime.now += 4
    session.detect_restart()
    assert session.last_runtime_status['retry_after_seconds'] == 1


@pytest.mark.parametrize('base,cap', [(0, 20), (-1, 20), (float('nan'), 20),
                                   (5, 4), (5, float('inf'))])
def test_invalid_backoff_configuration_rejected(base, cap):
    with pytest.raises(ValueError):
        PaperclipSession(config=OrchestratorConfig(retry_interval_seconds=base),
                         max_backoff_seconds=cap)


def test_max_backoff_defaults_from_config_when_omitted():
    # Issue #44: this used to be a hardcoded 3600 constant regardless of
    # config - an explicit caller value still wins, but omitting it must
    # read PAPERCLIP_MAX_BACKOFF_SECONDS.
    session = PaperclipSession(config=OrchestratorConfig(paperclip_max_backoff_seconds=99))
    assert session._max_backoff == 99
