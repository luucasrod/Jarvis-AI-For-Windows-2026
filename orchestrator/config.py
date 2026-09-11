"""Environment-based configuration for the orchestrator package.

Every value the orchestrator needs from the outside world (Telegram
credentials, the Paperclip URL, cycle timings) is read here from
environment variables, optionally loaded from a local `.env` file. Nothing
sensitive ever gets a hardcoded default - only URLs/timeouts/timings do.

`.env` is never committed (see .gitignore) - copy `.env.example` to `.env`
and fill in real values locally.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


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
    )


def validate_config(config: OrchestratorConfig | None = None) -> list[str]:
    """Returns a list of human-readable warnings for missing OPTIONAL
    configuration (e.g. Telegram not configured). Never raises - the
    orchestrator must keep working with Telegram/other integrations
    simply disabled when their config is absent. Extended by issue #16
    as more configuration keys are added in later waves."""
    cfg = config or load_config()
    warnings: list[str] = []
    if not cfg.telegram_bot_token:
        warnings.append("TELEGRAM_BOT_TOKEN nao configurado - Telegram desabilitado.")
    if not cfg.telegram_control_chat_id:
        warnings.append("TELEGRAM_CONTROL_CHAT_ID nao configurado - canal de controle desabilitado.")
    if not cfg.telegram_report_chat_id:
        warnings.append("TELEGRAM_REPORT_CHAT_ID nao configurado - canal de relatorio desabilitado.")
    return warnings
