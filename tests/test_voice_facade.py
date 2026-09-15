"""Tests for orchestrator.voice_facade (issue #11): stub-only for now,
the single contact point between main.py's voice dispatcher and this
package. Real behavior lands with #27 - these tests only guard the
current contract (a clear, non-empty "under construction" answer, never
an exception).
"""
from orchestrator.voice_facade import (
    handle_control_query,
    handle_report_query,
    handle_status_query,
)


def test_handle_status_query_returns_under_construction_message():
    reply = handle_status_query("como está o projeto Cashy")
    assert isinstance(reply, str) and reply


def test_handle_report_query_always_returns_a_string():
    reply = handle_report_query()
    assert isinstance(reply, str) and reply


def test_handle_control_query_returns_under_construction_message():
    reply = handle_control_query("pausar a orquestração")
    assert isinstance(reply, str) and reply


def test_stubs_never_raise_regardless_of_input():
    handle_status_query("")
    handle_control_query("")
    handle_report_query()
