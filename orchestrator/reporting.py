"""Daily report presentation and durable scheduler-event consumer (#32).

Call process_report_tick from the application's periodic loop. No thread or
voice runtime is started here. Uncertain Telegram deliveries require operator
reconciliation; retrying an unacknowledged POST could duplicate a report.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from orchestrator.config import load_config
from orchestrator.events import EventType, query_events
from orchestrator.orchestrator import run_report
from orchestrator.scheduler import Scheduler
from orchestrator.telegram_bot import send_report_message

_CURSOR = "orchestrator:last_report_at"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_reports (
    event_id INTEGER PRIMARY KEY,
    generated_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    pages TEXT NOT NULL,
    next_page INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
);
"""


def format_daily_report(report_data: dict) -> str:
    """Render #30's counts and task IDs, with optional task_titles lookup."""
    data = report_data
    count_fields = ("tasks_completed", "merges_completed", "deployments_finished",
                    "bugs_found", "tasks_blocked_since", "decisions_pending")
    list_fields = ("tasks_in_progress", "tasks_currently_blocked", "needs_lucas", "next_cycle")
    if not any(data.get(key) for key in count_fields + list_fields):
        return "Relatório diário: nenhuma atividade ou pendência neste período."

    titles = data.get("task_titles", {})

    def tasks(key):
        return "\n".join(f"- {titles.get(item, item)}" for item in data.get(key, [])) or "Nenhuma tarefa."

    return "\n\n".join([
        "RELATÓRIO DIÁRIO",
        f"CONCLUÍDO\n{data.get('tasks_completed', 0)} tarefa(s) concluída(s).",
        "EM ANDAMENTO\n" + tasks("tasks_in_progress"),
        f"MERGES\n{data.get('merges_completed', 0)} merge(s) concluído(s).",
        f"DEPLOYS\n{data.get('deployments_finished', 0)} deploy(s) finalizado(s).",
        f"BUGS\n{data.get('bugs_found', 0)} bug(s) encontrado(s).",
        f"BLOQUEIOS\n{data.get('tasks_blocked_since', 0)} bloqueio(s) registrado(s) no período.\n"
        + "Bloqueadas agora:\n" + tasks("tasks_currently_blocked"),
        "PRECISA DO LUCAS\n" + tasks("needs_lucas")
        + f"\n{data.get('decisions_pending', 0)} decisão(ões) do período ainda pendente(s).",
        "PRÓXIMO CICLO\n" + tasks("next_cycle"),
    ])


def paginate_report(text: str, limit: int = 4096) -> list[str]:
    """Lossless plain-text pages bounded conservatively by UTF-16 units."""
    if limit < 2:
        raise ValueError("limit must be at least 2")
    pages = []
    while text:
        units = end = 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end += 1
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline >= 0:
                end = newline + 1
        pages.append(text[:end])
        text = text[end:]
    return pages


@dataclass(frozen=True)
class ReportDelivery:
    event_id: int
    status: str
    confirmed_pages: int
    total_pages: int


def _first_pending(connection):
    return connection.execute(
        "SELECT e.id FROM events e LEFT JOIN daily_reports r ON r.event_id = e.id "
        "WHERE e.event_type = ? AND (r.status IS NULL OR r.status != 'delivered') "
        "ORDER BY e.id LIMIT 1", (EventType.REPORT_TIME_REACHED.value,),
    ).fetchone()


def process_report_tick(store, *, config=None, clock=None, send_fn=None) -> ReportDelivery | None:
    """Fire today's scheduler event, then deliver the oldest outstanding report.

    One report per call; subsequent ticks drain the backlog. Each page is claimed
    before I/O. A concurrent caller sees 'uncertain' and cannot send it again.
    Confirmed pages and the final collection cursor commit together. Missing
    configuration and explicit Telegram rejections can be retried; network errors,
    process crashes and ambiguous replies cannot be retried automatically.
    """
    config = config or load_config()
    now = clock() if clock else datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    Scheduler(store, config=config, clock=lambda: now).on_report_time()
    query_events(store, event_types=[EventType.REPORT_TIME_REACHED])  # ensure event schema
    store.ensure_schema(_SCHEMA)
    first = store.run_in_transaction(_first_pending)
    if first is None:
        return None
    event_id = first[0]
    if not config.telegram_bot_token or not config.telegram_report_chat_id:
        return ReportDelivery(event_id, "unconfigured", 0, 0)
    destination = hashlib.sha256(json.dumps([
        config.telegram_bot_token, config.telegram_report_chat_id,
    ]).encode()).hexdigest()
    rows = store.query("SELECT event_id FROM daily_reports WHERE event_id = ?", (event_id,))
    if not rows:
        data = run_report(store, clock=lambda: now)
        data["task_titles"] = {task.id: task.title for task in store.list_tasks()}
        pages = paginate_report(format_daily_report(data))

        def prepare(connection):
            if _first_pending(connection) != (event_id,):
                return
            connection.execute(
                "INSERT OR IGNORE INTO daily_reports(event_id, generated_at, destination, pages) "
                "VALUES (?, ?, ?, ?)",
                (event_id, data["generated_at"].isoformat(), destination, json.dumps(pages)),
            )
        store.run_in_transaction(prepare)

    def claim(connection):
        if _first_pending(connection) != (event_id,):
            return None
        row = connection.execute(
            "SELECT pages, next_page, status, destination FROM daily_reports WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        pages, index, status, target = row
        pages = json.loads(pages)
        if target != destination:
            if index == 0 and status == "pending":
                connection.execute("UPDATE daily_reports SET destination = ? WHERE event_id = ?", (destination, event_id))
            else:
                return ReportDelivery(event_id, "destination_changed", index, len(pages))
        if status != "pending":
            return ReportDelivery(event_id, status, index, len(pages))
        connection.execute("UPDATE daily_reports SET status = 'uncertain' WHERE event_id = ?", (event_id,))
        return pages, index

    while True:
        claimed = store.run_in_transaction(claim)
        if claimed is None or isinstance(claimed, ReportDelivery):
            return claimed
        pages, index = claimed
        try:
            ok, error = (send_fn or send_report_message)(pages[index], config=config, store=store)
        except Exception:
            return ReportDelivery(event_id, "uncertain", index, len(pages))
        if ok is not True:
            # These responses prove the existing transport did not deliver.
            retryable = isinstance(error, str) and error.startswith((
                "Telegram nao configurado", "token invalido", "chat_id invalido",
            ))
            status = "pending" if retryable else "uncertain"
            store.execute("UPDATE daily_reports SET status = ? WHERE event_id = ?", (status, event_id))
            return ReportDelivery(event_id, status, index, len(pages))

        def confirm(connection):
            final = index + 1 == len(pages)
            connection.execute(
                "UPDATE daily_reports SET next_page = ?, status = ? WHERE event_id = ?",
                (index + 1, "delivered" if final else "pending", event_id),
            )
            if final:
                generated = connection.execute(
                    "SELECT generated_at FROM daily_reports WHERE event_id = ?", (event_id,),
                ).fetchone()[0]
                previous = connection.execute("SELECT value FROM sync_state WHERE key = ?", (_CURSOR,)).fetchone()
                if previous is None or datetime.fromisoformat(previous[0]) < datetime.fromisoformat(generated):
                    # Same cursor contract as #30 mark_report_delivered, made
                    # atomic with delivery confirmation (no crash gap).
                    connection.execute(
                        "INSERT INTO sync_state(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_CURSOR, generated),
                    )
        store.run_in_transaction(confirm)
        if index + 1 == len(pages):
            return ReportDelivery(event_id, "delivered", len(pages), len(pages))
