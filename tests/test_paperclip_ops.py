"""Paperclip transport and persisted idempotency tests, no real network (#18)."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import paperclip_client as client
from orchestrator.config import OrchestratorConfig
from orchestrator.paperclip_ops import create_task_idempotent, find_created_task_id, get_task_status
from orchestrator.persistence import Store

CONFIG = OrchestratorConfig(paperclip_base_url='http://paperclip.invalid', paperclip_timeout_seconds=2.5)


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.text = 'response'
    def json(self):
        return self.payload


@pytest.fixture
def transport(monkeypatch):
    class Transport:
        def __init__(self):
            self.tasks, self.posts, self.gets = [], [], []
            self.get_error = self.post_error = None
            self.lose_response = False
        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            if self.get_error:
                raise self.get_error
            return Response(list(self.tasks))
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            if self.post_error:
                raise self.post_error
            created = {'id': f'pc-{len(self.posts)}', 'status': 'backlog', **kwargs['json']}
            self.tasks.append(created)
            if self.lose_response:
                raise requests.exceptions.Timeout()
            return Response(created, 201)
    fake = Transport()
    monkeypatch.setattr(client.requests, 'get', fake.get)
    monkeypatch.setattr(client.requests, 'post', fake.post)
    monkeypatch.setattr(client, '_CONFIG_TOKEN', '')
    monkeypatch.delenv('PAPERCLIP_API_TOKEN', raising=False)
    return fake


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / 'state.db')
    yield instance
    instance.close()


def create(store, **changes):
    args = dict(company_id='company', title='Task', description='Description',
                correlation_id='corr', store=store, config=CONFIG)
    args.update(changes)
    return create_task_idempotent(**args)


def test_create_twice_uses_one_post_and_configured_transport(store, transport):
    first = create(store, assignee_agent_id='agent')
    second = create(store, assignee_agent_id='agent')
    assert first['available'] and second['available']
    assert first['task_id'] == second['task_id'] == 'pc-1'
    assert second['cached']
    assert len(transport.posts) == 1
    url, kwargs = transport.posts[0]
    assert url == 'http://paperclip.invalid/api/companies/company/issues'
    assert kwargs['timeout'] == 2.5
    assert kwargs['json']['assigneeAgentId'] == 'agent'
    assert '<!-- jarvis-correlation:' in kwargs['json']['description']
    assert store.query('SELECT COUNT(*) FROM idempotency_keys')[0][0] == 1


def test_result_survives_restart_without_remote_call(tmp_path, transport):
    path = tmp_path / 'state.db'
    first = Store(path)
    result = create(first)
    first.close()
    second = Store(path)
    transport.get_error = requests.exceptions.ConnectionError()
    try:
        cached = create(second)
        assert cached['available'] and cached['task_id'] == result['task_id']
        assert len(transport.posts) == 1
    finally:
        second.close()


def test_offline_preflight_can_retry_without_losing_or_duplicating(store, transport):
    transport.get_error = requests.exceptions.ConnectionError()
    result = create(store)
    assert not result['available'] and result['reason'] == 'offline'
    assert not result['uncertain'] and transport.posts == []
    transport.get_error = None
    assert create(store)['available']
    assert len(transport.posts) == 1


def test_timeout_after_remote_commit_reconciles_on_restart(tmp_path, transport):
    path = tmp_path / 'state.db'
    first = Store(path)
    transport.lose_response = True
    result = create(first)
    assert result['reason'] == 'timeout' and result['uncertain']
    first.close()
    second = Store(path)
    try:
        result = create(second)
        assert result['available'] and result['reconciled']
        assert result['task_id'] == 'pc-1'
        assert len(transport.posts) == 1
    finally:
        second.close()


def test_uncertain_empty_result_never_blindly_posts_again(store, transport):
    transport.post_error = requests.exceptions.Timeout()
    assert create(store)['uncertain']
    transport.post_error = None
    result = create(store)
    assert not result['available'] and result['reason'] == 'creation_unconfirmed'
    assert len(transport.posts) == 1


def test_result_cache_repairs_missing_key_without_post(store, transport, monkeypatch):
    original = store.record_idempotency_key
    def fail(*args):
        raise sqlite3.OperationalError('simulated failure before key')
    monkeypatch.setattr(store, 'record_idempotency_key', fail)
    assert create(store)['reason'] == 'local_persistence_error'
    monkeypatch.setattr(store, 'record_idempotency_key', original)
    assert create(store)['cached']
    assert len(transport.posts) == 1
    assert len(store.query('SELECT * FROM idempotency_keys')) == 1


def test_failed_local_save_after_remote_success_reconciles(store, transport, monkeypatch):
    import orchestrator.paperclip_ops as ops
    original = ops._remember
    def fail(*args):
        raise sqlite3.OperationalError('simulated crash after POST')
    monkeypatch.setattr(ops, '_remember', fail)
    assert create(store)['uncertain']
    monkeypatch.setattr(ops, '_remember', original)
    assert create(store)['reconciled']
    assert len(transport.posts) == 1


def test_key_conflict_and_company_server_scoping(store, transport):
    assert create(store)['available']
    assert create(store, title='Different')['reason'] == 'correlation_conflict'
    assert create(store, company_id='another-company')['available']
    config = OrchestratorConfig(paperclip_base_url='http://other.invalid')
    assert create(store, config=config)['available']
    assert len(transport.posts) == 3


def test_ambiguous_remote_marker_does_not_create(store, transport):
    transport.lose_response = True
    create(store)
    transport.tasks.append({**transport.tasks[0], 'id': 'duplicate'})
    assert create(store)['reason'] == 'ambiguous_correlation'
    assert len(transport.posts) == 1


def test_concurrent_connections_claim_only_one_post(tmp_path, transport):
    stores = [Store(tmp_path / 'state.db') for _ in range(4)]
    barrier = Barrier(4)
    def execute(instance):
        barrier.wait(timeout=5)
        return create(instance)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(execute, stores))
        assert any(result['available'] for result in results)
        assert len(transport.posts) == 1
        assert create(stores[0])['available']
    finally:
        for instance in stores:
            instance.close()


def test_status_success_missing_and_invalid(store, transport):
    result = create(store)
    assert get_task_status('company', result['task_id'], config=CONFIG)['status'] == 'backlog'
    assert get_task_status('company', 'unknown', config=CONFIG)['reason'] == 'not_found'
    transport.tasks[0]['status'] = None
    assert get_task_status('company', result['task_id'], config=CONFIG)['reason'] == 'invalid_task_status'


@pytest.mark.parametrize('exception,reason', [(requests.exceptions.ConnectionError(), 'offline'),
                                            (requests.exceptions.Timeout(), 'timeout')])
def test_status_transport_errors(transport, exception, reason):
    transport.get_error = exception
    assert get_task_status('company', 'pc-1', config=CONFIG)['reason'] == reason


@pytest.mark.parametrize('payload', [None, {}, [None], [{'id': 123}]])
def test_malformed_list_does_not_crash_or_create(store, transport, monkeypatch, payload):
    monkeypatch.setattr(client.requests, 'get', lambda *args, **kwargs: Response(payload))
    assert create(store)['reason'] == 'invalid_task_list'
    assert get_task_status('company', 'id', config=CONFIG)['reason'] == 'invalid_task_list'
    assert transport.posts == []


def test_invalid_created_payload_is_uncertain(store, transport, monkeypatch):
    monkeypatch.setattr(client.requests, 'post', lambda *args, **kwargs: Response([], 201))
    assert create(store)['reason'] == 'invalid_created_task'
    assert create(store)['reason'] == 'creation_unconfirmed'


# --- find_created_task_id (issue #157) -----------------------------------------

def test_find_created_task_id_returns_none_before_any_creation(store, transport):
    assert find_created_task_id('company', 'corr', store=store, config=CONFIG) is None
    assert transport.gets == [] and transport.posts == []  # never touches the network


def test_find_created_task_id_returns_the_id_after_creation(store, transport):
    result = create(store)
    assert find_created_task_id('company', 'corr', store=store, config=CONFIG) == result['task_id']


def test_find_created_task_id_is_scoped_to_company_and_correlation(store, transport):
    create(store)
    assert find_created_task_id('other-company', 'corr', store=store, config=CONFIG) is None
    assert find_created_task_id('company', 'other-corr', store=store, config=CONFIG) is None


def test_find_created_task_id_never_raises_on_invalid_input(store, transport):
    assert find_created_task_id('', 'corr', store=store, config=CONFIG) is None
    assert find_created_task_id('company', '', store=store, config=CONFIG) is None
    assert find_created_task_id(None, 'corr', store=store, config=CONFIG) is None


def test_find_created_task_id_returns_none_on_local_persistence_error(store, transport, monkeypatch):
    create(store)
    monkeypatch.setattr(store, 'query', lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    assert find_created_task_id('company', 'corr', store=store, config=CONFIG) is None


def test_pagination_and_encoded_company_id(monkeypatch):
    paths = []
    def get(path, **kwargs):
        paths.append(path)
        offset = int(parse_qs(urlparse(path).query)['offset'][0])
        return ([{'id': str(i)} for i in range(100)] if offset == 0 else [{'id': 'last'}]), None
    monkeypatch.setattr(client, '_get', get)
    tasks, error = client.list_company_tasks('a/b?c', query='marker')
    assert error is None and len(tasks) == 101
    assert '/a%2Fb%3Fc/issues?' in paths[0]
    assert 'offset=100' in paths[1]
    assert 'q=marker' in paths[0]


def test_repeated_page_is_not_proof_of_absence(monkeypatch):
    monkeypatch.setattr(client, '_get', lambda *a, **k: ([{'id': str(i)} for i in range(100)], None))
    tasks, error = client.list_company_tasks('company')
    assert tasks is None and error == 'incomplete_task_list'


def test_generic_error_and_server_body_do_not_leak_credentials(transport, monkeypatch):
    monkeypatch.setenv('PAPERCLIP_API_TOKEN', 'fake-secret')
    transport.get_error = requests.exceptions.InvalidURL('fake-secret')
    assert client._get('/api/health')[1] == 'erro de rede'
    assert transport.gets[0][1]['headers']['Authorization'] == 'Bearer fake-secret'
    monkeypatch.setattr(client.requests, 'post', lambda *a, **k: Response({'error': 'fake-secret'}, 500))
    assert 'fake-secret' not in client._post('/api/test')[1]


@pytest.mark.parametrize('changes', [{'correlation_id': ''}, {'company_id': ''}, {'title': ''},
                                     {'description': None}, {'assignee_agent_id': []}])
def test_invalid_inputs_do_not_touch_network(store, transport, changes):
    assert not create(store, **changes)['available']
    assert transport.posts == [] and transport.gets == []
