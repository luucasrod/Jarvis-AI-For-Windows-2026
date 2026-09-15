"""Aggregated health reflects observations, not configuration alone (#36)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

import orchestrator.healthcheck as health
import paperclip_client
from orchestrator.config import OrchestratorConfig
from orchestrator.persistence import Store
from orchestrator.telegram_bot import send_control_message, send_report_message

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
CONFIG = OrchestratorConfig(telegram_bot_token='synthetic-secret', telegram_control_chat_id='111',
                            telegram_report_chat_id='222', paperclip_base_url='http://paperclip.invalid',
                            paperclip_timeout_seconds=2)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'state.db')
    yield value
    value.close()


def report(store, **kwargs):
    return health.get_health_status(store=store, config=kwargs.pop('config', CONFIG),
        clock=kwargs.pop('clock', lambda: NOW), paperclip_probe=kwargs.pop('paperclip_probe', lambda: True),
        github_probe=kwargs.pop('github_probe', lambda: True), **kwargs)


def seed_healthy(store):
    for component in ('jarvis', 'planner', 'scheduler'):
        health.record_heartbeat(store, component, at=NOW)
    for channel in ('control', 'report'):
        health.record_telegram_delivery(store, CONFIG, channel, True, at=NOW)
    store.set_sync_value('scheduler:2026-09-12:cycle_start', (NOW - timedelta(hours=4)).isoformat())


def test_all_subsystems_confirmed_and_human_readable(store):
    seed_healthy(store)
    result = report(store)
    assert result.ok and len(result.components) == 6
    assert result.last_cycle_at == NOW - timedelta(hours=4)
    assert 'Todos os subsistemas' in health.format_health_for_voice(result)
    rendered = health.format_health_for_telegram(result)
    assert 'Telegram: OK' in rendered and 'Acoes pendentes: 0' in rendered
    assert 'synthetic-secret' not in rendered and len(rendered) < 4096


def test_configured_but_unobserved_is_not_healthy(store):
    result = report(store)
    assert not result.ok and result.components['telegram'].status == 'unknown'
    assert all(result.components[name].status == 'unknown' for name in ('jarvis', 'planner', 'scheduler'))


def test_heartbeat_max_age_defaults_from_config_when_omitted(store):
    # Issue #44: heartbeat_max_age_seconds/telegram_max_age_seconds used
    # to be hardcoded 120/172800 constants regardless of config - a
    # heartbeat old enough to be stale under a TIGHTER configured
    # threshold, but that would still read as 'ok' under the old
    # hardcoded 120s default, proves the config value is actually used.
    for component in ('jarvis', 'planner', 'scheduler'):
        health.record_heartbeat(store, component, at=NOW - timedelta(seconds=100))
    tight_cfg = replace(CONFIG, heartbeat_max_age_seconds=50)
    result = report(store, config=tight_cfg)
    assert result.components['jarvis'].status == 'stale'


def test_paperclip_offline_does_not_hide_other_components(store):
    seed_healthy(store)
    result = report(store, paperclip_probe=lambda: False)
    assert not result.ok and result.components['paperclip'].status == 'offline'
    assert result.components['github'].status == 'ok'


def test_telegram_missing_credentials_and_changed_credentials(store):
    seed_healthy(store)
    assert report(store, config=replace(CONFIG, telegram_bot_token=None)).components['telegram'].status == 'unconfigured'
    assert report(store, config=replace(CONFIG, telegram_report_chat_id=None)).components['telegram'].status == 'unconfigured'
    assert report(store, config=replace(CONFIG, telegram_bot_token='new')).components['telegram'].status == 'unknown'


def test_new_failure_overrides_historical_telegram_success(store):
    seed_healthy(store)
    later = NOW + timedelta(seconds=1)
    health.record_telegram_delivery(store, CONFIG, 'report', False, at=later)
    result = report(store, clock=lambda: later)
    assert result.components['telegram'].status == 'offline'
    rows = dict(store.query('SELECT key,value FROM sync_state'))
    value = json.loads(rows[health._telegram_key(CONFIG, 'report')])
    assert value['success_at'] == NOW.isoformat()
    assert 'synthetic-secret' not in json.dumps(rows)


def test_old_telegram_observation_cannot_overwrite_newer_failure(store):
    seed_healthy(store)
    health.record_telegram_delivery(store, CONFIG, 'report', False, at=NOW + timedelta(seconds=2))
    health.record_telegram_delivery(store, CONFIG, 'report', True, at=NOW + timedelta(seconds=1))
    assert report(store, clock=lambda: NOW + timedelta(seconds=2)).components['telegram'].status == 'offline'


def test_expired_and_future_evidence_is_not_current_health(store):
    seed_healthy(store)
    result = report(store, clock=lambda: NOW + timedelta(days=3))
    assert result.components['planner'].status == 'stale'
    assert result.components['telegram'].status == 'unknown'
    assert report(store, clock=lambda: NOW - timedelta(seconds=1)).components['jarvis'].status == 'unknown'


def test_current_cycle_marker_alone_does_not_prove_live_scheduler(store):
    store.set_sync_value('scheduler:2026-09-12:cycle_start', NOW.isoformat())
    result = report(store)
    assert result.last_cycle_at == NOW and result.components['scheduler'].status == 'unknown'


def test_active_cooldowns_count_unknown_reset_but_expire_exactly(store):
    seed_healthy(store)
    store.set_rate_limit('Claude', 'test', (NOW + timedelta(seconds=1)).isoformat())
    store.set_rate_limit('Codex', 'test', NOW.isoformat())
    assert report(store).rate_limited_agents == ['Claude']
    store.set_rate_limit('Codex', 'test', None)
    assert report(store).rate_limited_agents == ['Claude', 'Codex']
    assert not report(store).ok


def test_pending_and_terminal_failures_count_real_client_tables(store):
    from orchestrator.github_client import _SCHEMA as github_schema
    from orchestrator.paperclip_ops import _SCHEMA as paperclip_schema
    seed_healthy(store)
    store.ensure_schema(github_schema)
    store.ensure_schema(paperclip_schema)
    for state in ('pending', 'reading', 'posting', 'done', 'failed'):
        store.execute('INSERT INTO pending_github_ops (operation_key,payload,status,next_attempt) VALUES (?,?,?,0)',
                      (state, '{}', state))
    store.execute('INSERT INTO paperclip_creations (operation_key,fingerprint,owner,result) VALUES (?,?,?,?)',
                  ('unfinished', 'f', 'o', None))
    store.execute('INSERT INTO paperclip_creations (operation_key,fingerprint,owner,result) VALUES (?,?,?,?)',
                  ('finished', 'f', 'o', '{}'))
    result = report(store)
    assert result.pending_actions == 4 and result.failed_actions == 1 and not result.ok
    assert '4 acao(oes) pendente(s)' in health.format_health_for_voice(result)


def test_storage_error_returns_unknown_not_false_zero_counts(store, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic-secret')
    monkeypatch.setattr(store, 'run_in_transaction', fail)
    result = report(store)
    assert not result.storage_ok and result.pending_actions is None and not result.ok
    assert 'synthetic-secret' not in health.format_health_for_telegram(result)


def test_probe_exception_never_surfaces_credentials(store):
    def fail():
        raise RuntimeError('https://api/botsynthetic-secret')
    result = report(store, github_probe=fail)
    assert result.components['github'].status == 'offline'
    assert 'synthetic-secret' not in health.format_health_for_voice(result)


@pytest.mark.parametrize('remaining,ok', [(123, True), (0, False), ('123', False), (True, False)])
def test_default_github_probe_uses_bounded_read_only_cli(monkeypatch, remaining, ok):
    def run(args, **kwargs):
        assert args == ['gh', 'api', '--hostname', 'github.com', '/rate_limit']
        assert kwargs['timeout'] == 10 and kwargs['capture_output'] and not kwargs.get('shell')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'resources': {'core': {'remaining': remaining}}}))
    monkeypatch.setattr(health.subprocess, 'run', run)
    assert health._probe_github() == ok


@pytest.mark.parametrize('payload,expected', [({'status': 'ok'}, True), ({}, False), ([], False), ({'status': 'error'}, False)])
def test_paperclip_health_validates_shape_and_configured_endpoint(monkeypatch, payload, expected):
    def get(url, **kwargs):
        assert url == 'http://paperclip.invalid/api/health' and kwargs['timeout'] == 2
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(paperclip_client.requests, 'get', get)
    assert paperclip_client.is_available(base_url=CONFIG.paperclip_base_url, timeout=2) == expected


@pytest.mark.parametrize('payload,expected', [({'ok': True, 'result': {'message_id': 1}}, True),
                                           ({'ok': False, 'description': 'synthetic-secret'}, False),
                                           ({}, False), ([], False)])
def test_sender_records_only_acknowledged_delivery(store, payload, expected):
    def post(*args, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: payload)
    control = send_control_message('message', CONFIG, post, store=store)
    send_report_message('message', CONFIG, post, store=store)
    assert control[0] == expected
    current = health.get_health_status(store=store, config=CONFIG,
                                      paperclip_probe=lambda: True, github_probe=lambda: True)
    assert current.components['telegram'].status == ('ok' if expected else 'offline')
    raw = str(store.query('SELECT * FROM sync_state'))
    assert 'synthetic-secret' not in raw and 'message' not in raw


def test_diagnostic_failure_does_not_turn_success_into_duplicate_send(store, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('diagnostic failure')
    monkeypatch.setattr(health, 'record_telegram_delivery', fail)
    result = send_control_message('once', CONFIG,
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {'ok': True, 'result': {}}), store=store)
    assert result == (True, None)


def test_persisted_observations_survive_reader_restart(tmp_path):
    path = tmp_path / 'health.db'
    first = Store(path)
    seed_healthy(first)
    first.close()
    second = Store(path)
    try:
        assert report(second).ok
    finally:
        second.close()
