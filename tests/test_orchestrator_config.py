"""Tests for orchestrator.config (issue #9)."""
import importlib

import orchestrator.config as config_module


def _reload_with_clean_env(monkeypatch, **env_overrides):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CONTROL_CHAT_ID",
        "TELEGRAM_REPORT_CHAT_ID",
        "PAPERCLIP_BASE_URL",
        "ORCHESTRATOR_TIMEZONE",
        "CYCLE_START_TIME",
        "CUTOFF_TIME",
        "REPORT_TIME",
        "RATE_LIMIT_BACKOFF_MINUTES",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


def test_defaults_when_env_absent(monkeypatch):
    mod = _reload_with_clean_env(monkeypatch)
    cfg = mod.load_config()
    assert cfg.telegram_bot_token is None
    assert cfg.telegram_control_chat_id is None
    assert cfg.telegram_report_chat_id is None
    assert cfg.paperclip_base_url == "http://127.0.0.1:3100"
    assert cfg.timezone == "Europe/Lisbon"
    assert cfg.cycle_start_time == "08:00"
    assert cfg.cutoff_time == "14:00"
    assert cfg.report_time == "17:00"
    assert cfg.rate_limit_backoff_minutes == 30


def test_env_vars_override_defaults(monkeypatch):
    mod = _reload_with_clean_env(
        monkeypatch,
        TELEGRAM_BOT_TOKEN="fake-token",
        TELEGRAM_CONTROL_CHAT_ID="111",
        TELEGRAM_REPORT_CHAT_ID="222",
        PAPERCLIP_BASE_URL="http://127.0.0.1:9999",
        ORCHESTRATOR_TIMEZONE="UTC",
        CYCLE_START_TIME="09:30",
        CUTOFF_TIME="15:00",
        REPORT_TIME="18:00",
        RATE_LIMIT_BACKOFF_MINUTES="45",
    )
    cfg = mod.load_config()
    assert cfg.telegram_bot_token == "fake-token"
    assert cfg.telegram_control_chat_id == "111"
    assert cfg.telegram_report_chat_id == "222"
    assert cfg.paperclip_base_url == "http://127.0.0.1:9999"
    assert cfg.timezone == "UTC"
    assert cfg.cycle_start_time == "09:30"
    assert cfg.cutoff_time == "15:00"
    assert cfg.report_time == "18:00"
    assert cfg.rate_limit_backoff_minutes == 45


def test_invalid_int_falls_back_to_default(monkeypatch):
    mod = _reload_with_clean_env(monkeypatch, RATE_LIMIT_BACKOFF_MINUTES="not-a-number")
    cfg = mod.load_config()
    assert cfg.rate_limit_backoff_minutes == 30


def test_validate_config_warns_on_missing_telegram(monkeypatch):
    mod = _reload_with_clean_env(monkeypatch)
    warnings = mod.validate_config(mod.load_config())
    assert any("TELEGRAM_BOT_TOKEN" in w for w in warnings)
    assert any("TELEGRAM_CONTROL_CHAT_ID" in w for w in warnings)
    assert any("TELEGRAM_REPORT_CHAT_ID" in w for w in warnings)


def test_validate_config_no_warnings_when_fully_configured(monkeypatch):
    mod = _reload_with_clean_env(
        monkeypatch,
        TELEGRAM_BOT_TOKEN="fake-token",
        TELEGRAM_CONTROL_CHAT_ID="111",
        TELEGRAM_REPORT_CHAT_ID="222",
    )
    warnings = mod.validate_config(mod.load_config())
    assert warnings == []


def test_dotenv_file_fills_missing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PAPERCLIP_BASE_URL=http://127.0.0.1:4242\n# comment\n\n", encoding="utf-8")

    for key in ("PAPERCLIP_BASE_URL",):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_module, "_ENV_FILE", env_file, raising=False)
    config_module._load_dotenv(env_file)

    assert config_module.os.environ.get("PAPERCLIP_BASE_URL") == "http://127.0.0.1:4242"
    del config_module.os.environ["PAPERCLIP_BASE_URL"]
