"""Tests for orchestrator.audit (issue #20)."""
from datetime import datetime, timedelta, timezone

from orchestrator.audit import query_audit, record, record_in_transaction
from orchestrator.persistence import Store


def test_record_normal_action(tmp_path):
    store = Store(tmp_path / "state.db")
    record(store, action="create_issue", origin="planner", result="success", project_id="cashy", correlation_id="corr-1")

    entries = query_audit(store)
    assert len(entries) == 1
    assert entries[0]["action"] == "create_issue"
    assert entries[0]["project_id"] == "cashy"
    assert entries[0]["origin"] == "planner"
    assert entries[0]["result"] == "success"
    assert entries[0]["correlation_id"] == "corr-1"
    store.close()


def test_secret_like_keys_are_redacted(tmp_path):
    store = Store(tmp_path / "state.db")
    record(
        store,
        action="call_api",
        origin="github_client",
        result="success",
        extra={"token": "gho_realsecretvalue", "TELEGRAM_BOT_TOKEN": "12345:abc", "password": "hunter2", "note": "ok"},
    )

    entries = query_audit(store)
    extra = entries[0]["extra"]
    assert extra["token"] == "***"
    assert extra["TELEGRAM_BOT_TOKEN"] == "***"
    assert extra["password"] == "***"
    assert extra["note"] == "ok"
    store.close()


def test_secret_marker_matches_case_insensitively_and_substrings():
    from orchestrator.audit import _redact_secrets

    redacted = _redact_secrets({"apiKey": "x", "user_secret_id": "y", "Senha": "z", "plain": "w"})
    assert redacted["apiKey"] == "***"
    assert redacted["user_secret_id"] == "***"
    assert redacted["Senha"] == "***"
    assert redacted["plain"] == "w"


# --- Regression tests from Codex's review (Review Task #103) --------------

def test_secret_nested_in_dict_is_redacted(tmp_path):
    store = Store(tmp_path / "state.db")
    record(
        store, action="call_api", origin="github_client", result="success",
        extra={"request": {"apiKey": "synthetic-secret-20"}},
    )

    row = store.query("SELECT extra FROM audit_log")[0][0]
    assert "synthetic-secret-20" not in row

    entries = query_audit(store)
    assert entries[0]["extra"]["request"]["apiKey"] == "***"
    store.close()


def test_secret_nested_in_list_of_dicts_is_redacted(tmp_path):
    store = Store(tmp_path / "state.db")
    record(
        store, action="call_api", origin="paperclip_ops", result="success",
        extra={"attempts": [{"nested": {"TELEGRAM_BOT_TOKEN": "synthetic-secret-20"}}]},
    )

    row = store.query("SELECT extra FROM audit_log")[0][0]
    assert "synthetic-secret-20" not in row

    entries = query_audit(store)
    assert entries[0]["extra"]["attempts"][0]["nested"]["TELEGRAM_BOT_TOKEN"] == "***"
    store.close()


def test_redact_secrets_does_not_mutate_original_input():
    from orchestrator.audit import _redact_secrets

    original = {"request": {"apiKey": "secret"}, "note": "ok"}
    _redact_secrets(original)
    assert original["request"]["apiKey"] == "secret"
    assert original["note"] == "ok"


def test_query_filters_by_project(tmp_path):
    store = Store(tmp_path / "state.db")
    record(store, action="a1", origin="o", result="r", project_id="cashy")
    record(store, action="a2", origin="o", result="r", project_id="argos")

    cashy_entries = query_audit(store, project_id="cashy")
    assert len(cashy_entries) == 1
    assert cashy_entries[0]["action"] == "a1"
    store.close()


def test_query_filters_by_since(tmp_path):
    store = Store(tmp_path / "state.db")
    record(store, action="a1", origin="o", result="r")

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert query_audit(store, since=future) == []

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert len(query_audit(store, since=past)) == 1
    store.close()


def test_record_without_extra_defaults_to_empty_dict(tmp_path):
    store = Store(tmp_path / "state.db")
    record(store, action="a1", origin="o", result="r")
    assert query_audit(store)[0]["extra"] == {}
    store.close()


# --- record_in_transaction (added for #37's atomic merge completion) ------

def test_record_in_transaction_commits_with_caller_transaction(tmp_path):
    store = Store(tmp_path / "state.db")

    def apply(connection):
        record_in_transaction(connection, action="auto_merge", origin="merge_policy", result="success",
                              correlation_id="corr-1", extra={"pr_number": 42})

    store.run_in_transaction(apply)

    entries = query_audit(store)
    assert len(entries) == 1
    assert entries[0]["action"] == "auto_merge"
    assert entries[0]["extra"] == {"pr_number": 42}
    store.close()


def test_record_in_transaction_rolls_back_with_caller_transaction(tmp_path):
    store = Store(tmp_path / "state.db")

    def apply(connection):
        record_in_transaction(connection, action="auto_merge", origin="merge_policy", result="success")
        raise ValueError("boom")

    try:
        store.run_in_transaction(apply)
    except ValueError:
        pass

    assert query_audit(store) == []
    store.close()


def test_record_in_transaction_redacts_secrets_like_record(tmp_path):
    store = Store(tmp_path / "state.db")

    def apply(connection):
        record_in_transaction(connection, action="a1", origin="o", result="r", extra={"api_token": "secret"})

    store.run_in_transaction(apply)

    assert query_audit(store)[0]["extra"] == {"api_token": "***"}
    store.close()
