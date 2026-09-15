"""Tests for orchestrator.config (issue #9)."""
import importlib
import logging

import pytest

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
        "SECONDBRAIN_INDEX_PATH",
        "RETRY_INTERVAL_SECONDS",
        "PAPERCLIP_TIMEOUT_SECONDS",
        "GITHUB_TIMEOUT_SECONDS",
        "GITHUB_MAX_BACKOFF_SECONDS",
        "PAPERCLIP_MAX_BACKOFF_SECONDS",
        "TELEGRAM_SEND_TIMEOUT_SECONDS",
        "TELEGRAM_POLL_TIMEOUT_SECONDS",
        "HEARTBEAT_MAX_AGE_SECONDS",
        "TELEGRAM_HEALTH_MAX_AGE_SECONDS",
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
    assert cfg.secondbrain_index_path == r"A:\SecondBrain\project_context_index.json"
    assert cfg.retry_interval_seconds == 30.0
    assert cfg.paperclip_timeout_seconds == 6.0
    assert cfg.github_timeout_seconds == 30.0
    assert cfg.github_max_backoff_seconds == 3600.0
    assert cfg.paperclip_max_backoff_seconds == 3600.0
    assert cfg.telegram_send_timeout_seconds == 10.0
    assert cfg.telegram_poll_timeout_seconds == 15.0
    assert cfg.heartbeat_max_age_seconds == 120.0
    assert cfg.telegram_health_max_age_seconds == 172800.0


def test_new_44_duration_settings_are_overridable(monkeypatch):
    mod = _reload_with_clean_env(
        monkeypatch,
        GITHUB_TIMEOUT_SECONDS="45",
        GITHUB_MAX_BACKOFF_SECONDS="1800",
        PAPERCLIP_MAX_BACKOFF_SECONDS="900",
        TELEGRAM_SEND_TIMEOUT_SECONDS="12",
        TELEGRAM_POLL_TIMEOUT_SECONDS="20",
        HEARTBEAT_MAX_AGE_SECONDS="60",
        TELEGRAM_HEALTH_MAX_AGE_SECONDS="3600",
    )
    cfg = mod.load_config()
    assert cfg.github_timeout_seconds == 45.0
    assert cfg.github_max_backoff_seconds == 1800.0
    assert cfg.paperclip_max_backoff_seconds == 900.0
    assert cfg.telegram_send_timeout_seconds == 12.0
    assert cfg.telegram_poll_timeout_seconds == 20.0
    assert cfg.heartbeat_max_age_seconds == 60.0
    assert cfg.telegram_health_max_age_seconds == 3600.0


def test_secondbrain_index_path_overridable(monkeypatch):
    mod = _reload_with_clean_env(monkeypatch, SECONDBRAIN_INDEX_PATH=r"C:\custom\index.json")
    cfg = mod.load_config()
    assert cfg.secondbrain_index_path == r"C:\custom\index.json"


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


def test_duration_overrides_and_complete_config_are_valid(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="orchestrator.config"):
        mod = _reload_with_clean_env(
            monkeypatch,
            TELEGRAM_BOT_TOKEN="fake-token-not-for-logs",
            TELEGRAM_CONTROL_CHAT_ID="111",
            TELEGRAM_REPORT_CHAT_ID="222",
            RETRY_INTERVAL_SECONDS="2.5",
            PAPERCLIP_TIMEOUT_SECONDS="0.75",
            SECONDBRAIN_INDEX_PATH=r"C:\projects\index.json",
        )
        cfg = mod.load_config()
        assert cfg.retry_interval_seconds == 2.5
        assert cfg.paperclip_timeout_seconds == 0.75
        assert cfg.secondbrain_index_path == r"C:\projects\index.json"
        assert mod.validate_config(cfg) == []
    assert caplog.records == []


@pytest.mark.parametrize("name,default", [
    ("RETRY_INTERVAL_SECONDS", 30.0),
    ("PAPERCLIP_TIMEOUT_SECONDS", 6.0),
    ("GITHUB_TIMEOUT_SECONDS", 30.0),
    ("GITHUB_MAX_BACKOFF_SECONDS", 3600.0),
    ("PAPERCLIP_MAX_BACKOFF_SECONDS", 3600.0),
    ("TELEGRAM_SEND_TIMEOUT_SECONDS", 10.0),
    ("TELEGRAM_POLL_TIMEOUT_SECONDS", 15.0),
    ("HEARTBEAT_MAX_AGE_SECONDS", 120.0),
    ("TELEGRAM_HEALTH_MAX_AGE_SECONDS", 172800.0),
])
@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "inf", "not-a-number"])
def test_invalid_durations_fall_back_and_log_names_only(monkeypatch, caplog, name, default, invalid):
    mod = _reload_with_clean_env(monkeypatch, **{name: invalid})
    cfg = mod.load_config()
    assert getattr(cfg, name.lower()) == default
    assert any(name in message for message in caplog.messages)
    assert all(invalid not in message for message in caplog.messages if invalid == "not-a-number")


def test_empty_dotenv_is_valid_and_missing_options_log_at_startup(tmp_path, monkeypatch, caplog):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    mod = _reload_with_clean_env(monkeypatch)
    mod._load_dotenv(env_file)
    warnings = mod.validate_config()
    assert len(warnings) == 3
    assert all(message in caplog.messages for message in warnings)
    assert mod.load_config().retry_interval_seconds == 30.0


def test_new_settings_dotenv_and_environment_precedence(tmp_path, monkeypatch):
    mod = _reload_with_clean_env(monkeypatch, RETRY_INTERVAL_SECONDS="7")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "RETRY_INTERVAL_SECONDS=90\nPAPERCLIP_TIMEOUT_SECONDS=4.5\n"
        "SECONDBRAIN_INDEX_PATH=C:/projects/index.json\n", encoding="utf-8",
    )
    # Register restoration for environment variables set by the loader.
    monkeypatch.setenv("PAPERCLIP_TIMEOUT_SECONDS", "")
    monkeypatch.delenv("PAPERCLIP_TIMEOUT_SECONDS")
    monkeypatch.setenv("SECONDBRAIN_INDEX_PATH", "")
    monkeypatch.delenv("SECONDBRAIN_INDEX_PATH")
    mod._load_dotenv(env_file)
    cfg = mod.load_config()
    assert cfg.retry_interval_seconds == 7.0
    assert cfg.paperclip_timeout_seconds == 4.5
    assert cfg.secondbrain_index_path == "C:/projects/index.json"
