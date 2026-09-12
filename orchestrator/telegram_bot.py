"""Telegram bot foundation: 2 separate channels (issue #19).

Deliberately built on raw `requests` calls against the Bot API instead of
a heavier library (python-telegram-bot etc): the project already uses
this exact pattern for Paperclip (paperclip_client.py) and it keeps
threading simple - Jarvis already runs voice I/O on its own threads, and
a full async framework would need its own event loop to coexist with
that. Documented here as the deliberate choice this issue asked for.

Two channels, never mixed (section 7/8):
  - CONTROL (TELEGRAM_CONTROL_CHAT_ID): bidirectional. Lucas sends
    objectives/decisions here; Jarvis sends blockers/NEEDS_LUCAS here.
  - REPORT (TELEGRAM_REPORT_CHAT_ID): mostly output, the daily summary.

Receiving is a single-poll function (`receive_control_updates`), not a
background loop - the actual repeated-polling loop belongs to whichever
issue wires the orchestrator's runtime (#23), so it can be started/
stopped/tested independently of Telegram itself.

Intent parsing of what Lucas's messages MEAN is out of scope (#24) - this
module only turns a raw incoming message into a `telegram_message_received`
event with the raw text. Report formatting is #25's job. NEEDS_LUCAS
message content is #26's job.
"""
from __future__ import annotations

from typing import Callable

import requests

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit
from orchestrator.persistence import Store

_API_BASE = "https://api.telegram.org/bot{token}"


def _url(token: str, method: str) -> str:
    return f"{_API_BASE.format(token=token)}/{method}"


def _send_message(
    chat_id: str | None,
    text: str,
    config: OrchestratorConfig,
    post_fn: Callable | None = None,
) -> tuple[bool, str | None]:
    if not config.telegram_bot_token or not chat_id:
        return False, "Telegram nao configurado (token ou chat_id ausente)"

    post = post_fn or requests.post
    try:
        response = post(
            _url(config.telegram_bot_token, "sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.exceptions.ConnectionError:
        return False, "offline"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception:  # never let a Telegram hiccup crash Jarvis
        # Never surface str(exc) here - requests embeds the full request
        # URL (which contains the bot token, e.g. InvalidURL) in several
        # of its exception messages, so anything more specific than a
        # generic label risks leaking the token into logs/UI (Review
        # Task #70).
        return False, "erro de rede"

    if response.status_code == 401:
        return False, "token invalido"
    if response.status_code == 400:
        return False, "chat_id invalido ou mensagem rejeitada"
    if response.status_code >= 500:
        return False, f"erro no servidor do Telegram ({response.status_code})"
    if response.status_code >= 400:
        return False, f"resposta inesperada ({response.status_code})"

    return True, None


def send_control_message(
    text: str, config: OrchestratorConfig | None = None, post_fn: Callable | None = None
) -> tuple[bool, str | None]:
    config = config or load_config()
    return _send_message(config.telegram_control_chat_id, text, config, post_fn)


def send_report_message(
    text: str, config: OrchestratorConfig | None = None, post_fn: Callable | None = None
) -> tuple[bool, str | None]:
    config = config or load_config()
    return _send_message(config.telegram_report_chat_id, text, config, post_fn)


def receive_control_updates(
    store: Store,
    config: OrchestratorConfig | None = None,
    last_update_id: int | None = None,
    get_fn: Callable | None = None,
) -> int | None:
    """Polls Telegram's getUpdates ONCE for new messages in the control
    channel, emits a telegram_message_received event per new message, and
    returns the update_id to pass as `last_update_id` on the next call
    (so messages are never processed twice). Messages from any chat other
    than the configured control chat are ignored (defense against a
    misconfigured or unexpected sender).

    Returns `last_update_id` unchanged (never raises) on any failure -
    Telegram being unreachable must never crash Jarvis."""
    config = config or load_config()
    if not config.telegram_bot_token or not config.telegram_control_chat_id:
        return last_update_id

    get = get_fn or requests.get
    params: dict = {"timeout": 0}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    try:
        response = get(_url(config.telegram_bot_token, "getUpdates"), params=params, timeout=15)
    except Exception:
        return last_update_id

    if response.status_code != 200:
        return last_update_id

    try:
        payload = response.json()
    except ValueError:
        return last_update_id

    if not isinstance(payload, dict):
        return last_update_id

    results = payload.get("result")
    if not isinstance(results, list):
        results = []
    new_last_update_id = last_update_id

    for update in results:
        # Telegram's own API contract guarantees objects here, but a
        # malformed/proxied response (null entries, null chat, etc.) must
        # degrade to "skip this update" rather than crash the whole poll
        # and lose the cursor for every update after it (Review Task #70).
        if not isinstance(update, dict):
            continue

        update_id = update.get("update_id")
        # Only a genuine int advances the cursor - a string/dict/list
        # here would crash `last_update_id + 1` on the NEXT poll (well
        # after this call already returned successfully), silently
        # bricking the offset for every update after it (Review Task
        # #70, 2nd revalidation). An invalid update_id keeps the last
        # known-safe cursor instead of adopting an untrustworthy one.
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            new_last_update_id = update_id

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue

        chat_id = str(chat.get("id", ""))
        if chat_id != str(config.telegram_control_chat_id):
            continue

        text = message.get("text", "")
        emit(
            store,
            EventType.TELEGRAM_MESSAGE_RECEIVED,
            {"chat_id": chat_id, "text": text, "update_id": update_id},
        )

    return new_last_update_id
