"""Environment-based configuration for the orchestrator package.

Every value the orchestrator needs from the outside world (Telegram
credentials, the Paperclip URL, cycle timings) is read here from
environment variables, optionally loaded from a local `.env` file. Nothing
sensitive ever gets a hardcoded default - only URLs/timeouts/timings do.

`.env` is never committed (see .gitignore) - copy `.env.example` to `.env`
and fill in real values locally.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"
_LOGGER = logging.getLogger(__name__)
_DEFAULT_RETRY_INTERVAL_SECONDS = 30.0
# Matches the existing Paperclip client's six-second request timeout.
_DEFAULT_PAPERCLIP_TIMEOUT_SECONDS = 6.0


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE per line, '#' comments, no quoting
    edge cases. Only sets a variable if it is not already present in the
    real environment (real env always wins over the file)."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(_ENV_FILE)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _positive_seconds(value: str | None) -> float | None:
    """Reject zero, negative and non-finite durations before client use."""
    try:
        seconds = float(value) if value else 0.0
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _get_seconds(name: str, default: float) -> float:
    parsed = _positive_seconds(_get(name))
    return parsed if parsed is not None else default


@dataclass(frozen=True)
class OrchestratorConfig:
    telegram_bot_token: str | None = field(default=None)
    telegram_control_chat_id: str | None = field(default=None)
    telegram_report_chat_id: str | None = field(default=None)
    paperclip_base_url: str = "http://127.0.0.1:3100"
    timezone: str = "Europe/Lisbon"
    cycle_start_time: str = "08:00"
    cutoff_time: str = "14:00"
    report_time: str = "17:00"
    rate_limit_backoff_minutes: int = 30
    idle_check_minutes: int = 15
    secondbrain_index_path: str = r"A:\SecondBrain\project_context_index.json"
    retry_interval_seconds: float = _DEFAULT_RETRY_INTERVAL_SECONDS
    paperclip_timeout_seconds: float = _DEFAULT_PAPERCLIP_TIMEOUT_SECONDS


def load_config() -> OrchestratorConfig:
    """Builds an OrchestratorConfig from the current environment (after
    .env has been merged in). Safe to call repeatedly; re-reads env each
    time so tests can monkeypatch os.environ between calls."""
    return OrchestratorConfig(
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_control_chat_id=_get("TELEGRAM_CONTROL_CHAT_ID"),
        telegram_report_chat_id=_get("TELEGRAM_REPORT_CHAT_ID"),
        paperclip_base_url=_get("PAPERCLIP_BASE_URL", "http://127.0.0.1:3100"),
        timezone=_get("ORCHESTRATOR_TIMEZONE", "Europe/Lisbon"),
        cycle_start_time=_get("CYCLE_START_TIME", "08:00"),
        cutoff_time=_get("CUTOFF_TIME", "14:00"),
        report_time=_get("REPORT_TIME", "17:00"),
        rate_limit_backoff_minutes=_get_int("RATE_LIMIT_BACKOFF_MINUTES", 30),
        idle_check_minutes=_get_int("IDLE_CHECK_MINUTES", 15),
        secondbrain_index_path=_get(
            "SECONDBRAIN_INDEX_PATH", r"A:\SecondBrain\project_context_index.json"
        ),
        retry_interval_seconds=_get_seconds(
            "RETRY_INTERVAL_SECONDS", _DEFAULT_RETRY_INTERVAL_SECONDS
        ),
        paperclip_timeout_seconds=_get_seconds(
            "PAPERCLIP_TIMEOUT_SECONDS", _DEFAULT_PAPERCLIP_TIMEOUT_SECONDS
        ),
    )


def validate_config(config: OrchestratorConfig | None = None) -> list[str]:
    """Log and return configuration warnings without requiring credentials.

    All current settings are optional or have defaults; there are no
    mandatory startup secrets. Existing callers still receive the warning
    list. Raw environment values (including tokens) are never logged.
    Called once when this configuration module is first imported at
    startup; callers may also revalidate after changing configuration.
    """
    cfg = config or load_config()
    warnings: list[str] = []
    if not cfg.telegram_bot_token:
        warnings.append("TELEGRAM_BOT_TOKEN nao configurado - Telegram desabilitado.")
    if not cfg.telegram_control_chat_id:
        warnings.append("TELEGRAM_CONTROL_CHAT_ID nao configurado - canal de controle desabilitado.")
    if not cfg.telegram_report_chat_id:
        warnings.append("TELEGRAM_REPORT_CHAT_ID nao configurado - canal de relatorio desabilitado.")
    for name, default in (
        ("RETRY_INTERVAL_SECONDS", _DEFAULT_RETRY_INTERVAL_SECONDS),
        ("PAPERCLIP_TIMEOUT_SECONDS", _DEFAULT_PAPERCLIP_TIMEOUT_SECONDS),
    ):
        raw = _get(name)
        if raw is not None and _positive_seconds(raw) is None:
            warnings.append(
                f"{name} deve ser um numero finito maior que zero - "
                f"usando default de {default:g} segundos ao carregar configuracao."
            )
    for warning in warnings:
        _LOGGER.warning(warning)
    return warnings


# Configuration is imported by orchestrator entry points; validate here
# without importing the voice monolith or starting a background service.
validate_config()
