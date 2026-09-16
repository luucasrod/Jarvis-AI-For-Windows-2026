"""Persistent Telegram <-> orchestrator bridge (issue #149 follow-up).

Every piece this module glues together already existed and was tested in
isolation: #19/#148's `receive_control_updates` (poll + transcribe + emit
an event), #31/#149's `handle_control_message` (structured grammar + free
conversation), and #32's `process_report_tick` (fires the 17:00 daily
report once local time crosses it). Nothing here had ever actually been
WIRED into a running loop - #23's own note in telegram_bot.py explicitly
left "the actual repeated-polling loop" for whichever issue wires the
runtime. Without this, every prior "live test" only worked because a
human ran a one-off script by hand for that single message.

Real GitHub/Paperclip dispatch (issue #152) is wired in too, but ONLY
behind the user's own explicit "sim" confirming a specific proposed plan
- see decisions.py's `confirm_fn` contract and plan_confirmation.py's
`execute_confirmed_plan`. This loop never dispatches anything on its own.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from functools import partial
from typing import Callable

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.conversation import answer_free_text
from orchestrator.decisions import handle_control_message
from orchestrator.events import EventType, query_events
from orchestrator.persistence import Store
from orchestrator.plan_confirmation import execute_confirmed_plan
from orchestrator.reporting import process_report_tick
from orchestrator.telegram_bot import receive_control_updates, send_control_message

_LOGGER = logging.getLogger(__name__)
_DEFAULT_POLL_INTERVAL_SECONDS = 3.0


def _process_new_control_messages(
    store: Store, config: OrchestratorConfig, *,
    poll_started_at: datetime, previous_update_id: int | None,
    send_fn: Callable | None, fallback_fn: Callable | None, confirm_fn: Callable | None = None,
) -> None:
    """`receive_control_updates` only emits an event per message (#19's own
    deliberate scope boundary - it does not interpret meaning). This reads
    back exactly the messages THAT call just emitted - filtered by
    `update_id > previous_update_id`, never by count or by a second
    cursor of its own, so a message can never be silently skipped OR
    replayed by this specific step even if unrelated events land in the
    same window - and routes each one through #31/#149's real grammar +
    free-conversation handling."""
    events = query_events(store, since=poll_started_at, event_types=[EventType.TELEGRAM_MESSAGE_RECEIVED])
    for event in events:
        payload = event.get("payload") or {}
        update_id = payload.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            continue
        if previous_update_id is not None and update_id <= previous_update_id:
            continue
        text = payload.get("text") or ""
        if not text:
            continue
        try:
            handle_control_message(
                text, store=store,
                send_fn=send_fn or partial(send_control_message, config=config),
                fallback_fn=fallback_fn or partial(answer_free_text, store=store, config=config),
                confirm_fn=confirm_fn or partial(execute_confirmed_plan, store=store, config=config),
            )
        except Exception:
            # A single malformed/unlucky message must never stop the loop
            # from processing the rest of this batch or the next poll.
            _LOGGER.exception("Falha ao processar mensagem de controle (update_id=%s)", update_id)


def run_forever(
    *, store: Store | None = None, config: OrchestratorConfig | None = None,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    iterations: int | None = None,
    send_fn: Callable | None = None, fallback_fn: Callable | None = None,
    confirm_fn: Callable | None = None,
    report_send_fn: Callable | None = None,
    get_fn: Callable | None = None, transcribe_fn: Callable | None = None,
) -> None:
    """Polls the control channel and ticks the daily report forever (or
    for `iterations` cycles, for tests/manual runs). Never raises out of
    the loop body - Telegram/Groq/network trouble degrades to "try again
    next tick", matching every function this composes, which already
    never raises on its own.
    """
    cfg = config or load_config()
    owned = store is None
    active_store = store or Store()
    last_update_id: int | None = None
    count = 0
    try:
        while iterations is None or count < iterations:
            poll_started_at = datetime.now(timezone.utc)
            previous_update_id = last_update_id
            try:
                last_update_id = receive_control_updates(
                    active_store, config=cfg, last_update_id=last_update_id,
                    get_fn=get_fn, transcribe_fn=transcribe_fn,
                )
            except Exception:
                _LOGGER.exception("Falha ao consultar getUpdates do canal de controle")
                last_update_id = previous_update_id

            if last_update_id != previous_update_id:
                try:
                    _process_new_control_messages(
                        active_store, cfg, poll_started_at=poll_started_at,
                        previous_update_id=previous_update_id, send_fn=send_fn, fallback_fn=fallback_fn,
                        confirm_fn=confirm_fn,
                    )
                except Exception:
                    # query_events/sqlite trouble here must not kill the
                    # whole "forever" loop - same resilience contract as
                    # the getUpdates and report-tick calls above/below.
                    # Rolling last_update_id back (matching the getUpdates
                    # failure path above) means the NEXT poll re-fetches
                    # and retries this same batch instead of silently
                    # skipping it forever - a duplicate internal event on
                    # retry is far cheaper than a message nobody ever
                    # answers.
                    _LOGGER.exception("Falha ao processar mensagens de controle deste ciclo")
                    last_update_id = previous_update_id

            try:
                process_report_tick(active_store, config=cfg, send_fn=report_send_fn)
            except Exception:
                _LOGGER.exception("Falha ao processar o relatorio diario")

            count += 1
            if iterations is None or count < iterations:
                time.sleep(poll_interval_seconds)
    finally:
        if owned:
            active_store.close()


if __name__ == "__main__":
    # `python -m orchestrator.runtime` (or `python orchestrator/runtime.py`
    # from the repo root) starts the bridge and runs until interrupted.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _LOGGER.info("Jarvis <-> Telegram bridge iniciando (Ctrl+C para parar)...")
    run_forever()
