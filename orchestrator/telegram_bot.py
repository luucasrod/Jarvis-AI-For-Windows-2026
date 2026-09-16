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

import sqlite3
from typing import Callable

import requests

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit
from orchestrator.persistence import Store

_API_BASE = "https://api.telegram.org/bot{token}"
_FILE_BASE = "https://api.telegram.org/file/bot{token}/{path}"


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
            timeout=config.telegram_send_timeout_seconds,
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

    try:
        payload = response.json()
    except (ValueError, TypeError):
        return False, 'resposta invalida do Telegram'
    if not isinstance(payload, dict) or payload.get('ok') is not True or not isinstance(payload.get('result'), dict):
        return False, 'envio nao confirmado pelo Telegram'
    return True, None


def _record_delivery(store, config, channel, result):
    if store is not None:
        from orchestrator.healthcheck import record_telegram_delivery
        try:
            record_telegram_delivery(store, config, channel, result[0])
        except sqlite3.Error:
            # A diagnostic write failure must not turn a confirmed send into
            # a retryable delivery failure (and duplicate the user's message).
            pass
    return result


def send_control_message(
    text: str, config: OrchestratorConfig | None = None, post_fn: Callable | None = None,
    *, store: Store | None = None,
) -> tuple[bool, str | None]:
    config = config or load_config()
    return _record_delivery(store, config, 'control',
                            _send_message(config.telegram_control_chat_id, text, config, post_fn))


def send_report_message(
    text: str, config: OrchestratorConfig | None = None, post_fn: Callable | None = None,
    *, store: Store | None = None,
) -> tuple[bool, str | None]:
    config = config or load_config()
    return _record_delivery(store, config, 'report',
                            _send_message(config.telegram_report_chat_id, text, config, post_fn))


def _download_telegram_file(
    file_id: str, config: OrchestratorConfig, get_fn: Callable,
) -> bytes | None:
    """Resolves a Telegram file_id to its bytes via getFile + the file
    download endpoint. Returns None on any failure - a voice message that
    can't be fetched degrades to "no text this update", never a crash."""
    try:
        info = get_fn(
            _url(config.telegram_bot_token, "getFile"),
            params={"file_id": file_id}, timeout=config.telegram_poll_timeout_seconds,
        )
        if info.status_code != 200:
            return None
        data = info.json()
        if not isinstance(data, dict):
            return None
        file_path = (data.get("result") or {}).get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return None
        content = get_fn(
            _FILE_BASE.format(token=config.telegram_bot_token, path=file_path),
            params=None, timeout=config.telegram_poll_timeout_seconds,
        )
        if content.status_code != 200:
            return None
        return content.content
    except Exception:
        return None


def _real_groq_client(api_key: str):
    from groq import Groq

    return Groq(api_key=api_key)


def _transcribe_telegram_voice(
    file_id: str, config: OrchestratorConfig, get_fn: Callable,
    groq_client_factory: Callable[[str], object] | None = None,
) -> str | None:
    """Downloads a Telegram voice/audio message and transcribes it via
    Groq Whisper - the SAME model/language main.py's own take_command()
    already uses for local microphone input (issue #148), so a command
    sent by voice through the control channel is transcribed consistently
    with the rest of Jarvis. Returns None (never raises) on any failure:
    missing GROQ_API_KEY, unreachable file, unreachable Groq (including
    the `groq` package not being installed at all - this orchestrator
    package's own test/CI environment deliberately doesn't carry it, only
    main.py's runtime venv does), or empty transcription - the caller
    treats that exactly like a message with no text at all.

    `groq_client_factory` defaults to constructing a real `groq.Groq`
    client (imported lazily, only when actually needed) - injected in
    tests so they never need the `groq` package importable at all."""
    if not config.groq_api_key:
        return None
    audio_bytes = _download_telegram_file(file_id, config, get_fn)
    if not audio_bytes:
        return None
    try:
        make_client = groq_client_factory or _real_groq_client
        client = make_client(config.groq_api_key)
        result = client.audio.transcriptions.create(
            file=("voice.ogg", audio_bytes),
            model=config.groq_transcribe_model,
            language="pt",
            response_format="text",
        )
        text = (result if isinstance(result, str) else getattr(result, "text", "")).strip()
        return text or None
    except Exception:
        return None


def receive_control_updates(
    store: Store,
    config: OrchestratorConfig | None = None,
    last_update_id: int | None = None,
    get_fn: Callable | None = None,
    transcribe_fn: Callable | None = None,
) -> int | None:
    """Polls Telegram's getUpdates ONCE for new messages in the control
    channel, emits a telegram_message_received event per new message, and
    returns the update_id to pass as `last_update_id` on the next call
    (so messages are never processed twice). Messages from any chat other
    than the configured control chat are ignored (defense against a
    misconfigured or unexpected sender).

    A message with no `text` but a `voice`/`audio` attachment (issue #148)
    is transcribed via `transcribe_fn` (defaults to
    `_transcribe_telegram_voice`, Groq Whisper) and the transcribed text
    is emitted exactly like a typed message - the downstream grammar
    parser (#31's `decisions.handle_control_message`) never knows the
    difference. A voice message that fails to download/transcribe (no
    GROQ_API_KEY, bad audio, Groq unreachable) is emitted with empty
    text, same as any other message the parser can't make sense of -
    never a crash, never a lost cursor.

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
        response = get(_url(config.telegram_bot_token, "getUpdates"), params=params, timeout=config.telegram_poll_timeout_seconds)
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
        # An update_id that isn't a genuine, strictly-increasing int is
        # never trustworthy enough to act on - skip the WHOLE update
        # (never just the cursor assignment), so it can neither crash the
        # next poll's `last_update_id + 1` (2nd revalidation) nor let an
        # old/duplicate/regressed id re-deliver a command as if it were
        # new, nor get emitted as a message event at all (Review Task
        # #70, 3rd revalidation).
        valid_id = (
            isinstance(update_id, int)
            and not isinstance(update_id, bool)
            and (new_last_update_id is None or update_id > new_last_update_id)
        )
        if not valid_id:
            continue
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
        transcribed = False
        if not text:
            voice = message.get("voice") or message.get("audio")
            file_id = voice.get("file_id") if isinstance(voice, dict) else None
            if isinstance(file_id, str) and file_id:
                transcribe = transcribe_fn or _transcribe_telegram_voice
                text = transcribe(file_id, config, get) or ""
                transcribed = bool(text)
        emit(
            store,
            EventType.TELEGRAM_MESSAGE_RECEIVED,
            {"chat_id": chat_id, "text": text, "update_id": update_id, "transcribed": transcribed},
        )

    return new_last_update_id
