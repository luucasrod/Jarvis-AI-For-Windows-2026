"""Tests for paperclip_client.list_companies (issue #152)."""
import paperclip_client


def test_list_companies_returns_id_and_name(monkeypatch):
    monkeypatch.setattr(
        paperclip_client, "_get",
        lambda path, **kwargs: ([{"id": "c1", "name": "Hub"}, {"id": "c2", "name": "Argos"}], None),
    )
    companies, err = paperclip_client.list_companies()
    assert err is None
    assert companies == [{"id": "c1", "name": "Hub"}, {"id": "c2", "name": "Argos"}]


def test_list_companies_drops_entries_missing_an_id(monkeypatch):
    monkeypatch.setattr(
        paperclip_client, "_get",
        lambda path, **kwargs: ([{"id": "c1", "name": "Hub"}, {"name": "sem id"}, "not a dict"], None),
    )
    companies, err = paperclip_client.list_companies()
    assert err is None
    assert companies == [{"id": "c1", "name": "Hub"}]


def test_list_companies_propagates_the_error_reason(monkeypatch):
    monkeypatch.setattr(paperclip_client, "_get", lambda path, **kwargs: (None, "offline"))
    companies, err = paperclip_client.list_companies()
    assert companies == [] and err == "offline"


def test_list_companies_handles_unexpected_shape(monkeypatch):
    monkeypatch.setattr(paperclip_client, "_get", lambda path, **kwargs: ({"not": "a list"}, None))
    companies, err = paperclip_client.list_companies()
    assert companies == [] and err is not None
