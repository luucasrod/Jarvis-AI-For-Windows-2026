from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import sqlite3

import pytest
import requests

from orchestrator import reporting, telegram_bot
from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, emit
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store

NOW = datetime(2026, 9, 14, 17, tzinfo=timezone.utc)
CFG = OrchestratorConfig(telegram_bot_token="test-only", telegram_report_chat_id="report",
                         telegram_control_chat_id="control", timezone="UTC")


def tick(store, send, **kwargs):
    return reporting.process_report_tick(store, config=kwargs.pop("config", CFG),
                                         clock=kwargs.pop("clock", lambda: NOW), send_fn=send, **kwargs)


def test_format_all_sections():
    text = reporting.format_daily_report(dict(
        tasks_completed=3, tasks_in_progress=["a"], merges_completed=2,
        deployments_finished=1, bugs_found=4, tasks_blocked_since=5,
        tasks_currently_blocked=["b"], needs_lucas=["c"], decisions_pending=6,
        next_cycle=["d"], task_titles={"a": "Construir painel", "b": "API indisponível",
                                      "c": "Escolher domínio", "d": "Testar deploy"}))
    for section in ("CONCLUÍDO", "EM ANDAMENTO", "MERGES", "DEPLOYS", "BUGS",
                    "BLOQUEIOS", "PRECISA DO LUCAS", "PRÓXIMO CICLO"):
        assert section in text
    assert "Construir painel" in text and "Escolher domínio" in text
    assert "3 tarefa(s) concluída(s)" in text and "6 decisão(ões)" in text
    assert "{" not in text


def test_empty_and_sparse_reports():
    assert len(reporting.format_daily_report({})) < 100
    assert "nenhuma atividade" in reporting.format_daily_report({})
    text = reporting.format_daily_report({"bugs_found": 1})
    assert "1 bug(s)" in text and "Nenhuma tarefa." in text
    assert "nenhuma atividade" not in reporting.format_daily_report({"next_cycle": ["Revisar PR"]})


@pytest.mark.parametrize("text", ["á" * 9000, "🛰" * 5000, ("Uma linha\n" * 1000), "x" * 4096],
                         ids=["accents", "astral", "lines", "boundary"])
def test_pages_preserve_entire_text_within_limit(text):
    pages = reporting.paginate_report(text)
    assert "".join(pages) == text
    assert all(0 < len(page.encode("utf-16-le")) // 2 <= 4096 for page in pages)


def test_scheduler_time_channel_and_reopened_store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    store = Store(path)
    posts = []

    class Reply:
        status_code = 200
        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    def post(url, **kwargs):
        posts.append(kwargs["json"])
        return Reply()

    monkeypatch.setattr(telegram_bot.requests, "post", post)
    assert tick(store, None, clock=lambda: NOW - timedelta(minutes=1)) is None
    assert tick(store, None).status == "delivered"
    assert posts[0]["chat_id"] == "report"
    assert "nenhuma atividade" in posts[0]["text"]
    assert store.get_sync_value("orchestrator:last_report_at") == NOW.isoformat()
    store.close()
    store = Store(path)
    assert tick(store, None) is None
    assert len(posts) == 1
    store.close()


def test_partial_delivery_retries_only_rejected_page_and_freezes_content(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.save_task(Task(title="A" * 9000, objective="report", state=TaskState.IN_PROGRESS))
    sent = []
    def send(text, **kwargs):
        sent.append(text)
        return (True, None) if len(sent) == 1 else (False, "chat_id invalido ou mensagem rejeitada")
    first = tick(store, send)
    assert first.confirmed_pages == 1 and first.status == "pending"
    assert store.get_sync_value("orchestrator:last_report_at") is None
    task = store.list_tasks()[0]
    task.title = "changed after snapshot"
    store.save_task(task)
    store.close()
    store = Store(path)
    resumed = []
    def success(text, **kwargs):
        resumed.append(text)
        return True, None
    assert tick(store, success, clock=lambda: NOW + timedelta(hours=1)).status == "delivered"
    assert resumed[0] == sent[1]
    assert "changed after snapshot" not in "".join(resumed)
    assert store.get_sync_value("orchestrator:last_report_at") == NOW.isoformat()
    store.close()


def test_timeout_after_remote_acceptance_never_reposts(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.ReadTimeout("accepted but response lost")
    monkeypatch.setattr(telegram_bot.requests, "post", post)
    store = Store(path)
    assert tick(store, None).status == "uncertain"
    store.close()
    store = Store(path)
    assert tick(store, None).status == "uncertain"
    assert store.get_sync_value("orchestrator:last_report_at") is None
    assert len(calls) == 1
    store.close()


def test_crash_after_send_before_confirmation_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    calls = []
    def send(text, **kwargs):
        calls.append(text)
        store.execute("CREATE TRIGGER crash_confirm BEFORE UPDATE OF next_page ON daily_reports "
                      "BEGIN SELECT RAISE(ABORT, 'crash'); END")
        return True, None
    with pytest.raises(sqlite3.IntegrityError):
        tick(store, send)
    store.close()
    store = Store(path)
    store.execute("DROP TRIGGER crash_confirm")
    assert tick(store, send).status == "uncertain"
    assert len(calls) == 1
    assert store.get_sync_value("orchestrator:last_report_at") is None
    store.close()


def test_two_connections_only_one_post(tmp_path):
    one, two = Store(tmp_path / "db"), Store(tmp_path / "db")
    entered, release = Event(), Event()
    calls = []
    def send(text, **kwargs):
        calls.append(text)
        entered.set()
        assert release.wait(5)
        return True, None
    with ThreadPoolExecutor() as pool:
        future = pool.submit(tick, one, send)
        try:
            assert entered.wait(5)
            assert tick(two, send).status == "uncertain"
        finally:
            release.set()
        assert future.result().status == "delivered"
    assert len(calls) == 1
    one.close()
    two.close()


def test_config_recovery_and_uncertain_destination_change(tmp_path):
    store = Store(tmp_path / "db")
    calls = []
    def rejected(text, **kwargs):
        calls.append(text)
        return False, "token invalido"
    assert tick(store, rejected, config=replace(CFG, telegram_bot_token=None)).status == "unconfigured"
    assert not calls
    assert tick(store, rejected).status == "pending"
    changed = replace(CFG, telegram_bot_token="replacement-test-only")
    assert tick(store, lambda *a, **k: (False, "timeout"), config=changed).status == "uncertain"
    assert tick(store, rejected).status == "destination_changed"
    assert len(calls) == 1
    store.close()


def test_existing_scheduler_event_consumed_before_next_time(tmp_path):
    store = Store(tmp_path / "db")
    emit(store, EventType.REPORT_TIME_REACHED, created_at=NOW - timedelta(days=1))
    result = tick(store, lambda *a, **k: (True, None), clock=lambda: NOW - timedelta(hours=1))
    assert result.status == "delivered"
    store.close()


def test_uncertain_old_report_blocks_later_event_without_cursor_advance(tmp_path):
    store = Store(tmp_path / "db")
    first = tick(store, lambda *a, **k: (False, "timeout"))
    later = tick(store, lambda *a, **k: pytest.fail("must not send"), clock=lambda: NOW + timedelta(days=1))
    assert later.event_id == first.event_id and later.status == "uncertain"
    assert store.get_sync_value("orchestrator:last_report_at") is None
    store.close()
