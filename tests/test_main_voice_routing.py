"""Real routing tests for the voice_facade intents added to main.py (#11),
verified against Review Task #139's 3 findings.

main.py cannot be imported directly in this test environment (it pulls in
sounddevice/audio hardware deps this CI has no need to install for a text-
routing test) - so this extracts the SPECIFIC real AST nodes
(process_command, _looks_like_paperclip_query, and every trigger-phrase
constant they reference) straight from the actual source file and execs
them in an isolated namespace with fakes standing in for every side-
effecting name process_command's top branches call (say, paperclip_report,
voice_facade, _log). This exercises the REAL routing logic, not a
reimplementation of it - a regression in main.py's actual source fails
these tests.
"""
import ast
from pathlib import Path

import pytest

_MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"

_WANTED_NAMES = {
    "_PAPERCLIP_TRIGGER_PHRASES",
    "_looks_like_paperclip_query",
    "_ORCHESTRATOR_STATUS_PHRASES",
    "_ORCHESTRATOR_REPORT_PHRASES",
    "_ORCHESTRATOR_CONTROL_PHRASES",
    "_ORCHESTRATOR_UNAVAILABLE",
    "process_command",
}


def _extract_module():
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"), filename=str(_MAIN_PY))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED_NAMES:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(t in _WANTED_NAMES for t in targets):
                selected.append(node)
    found = set()
    for node in selected:
        found.update(t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name))
        if isinstance(node, ast.FunctionDef):
            found.add(node.name)
    missing = _WANTED_NAMES - found
    assert not missing, f"main.py no longer defines: {missing} - update _WANTED_NAMES/test"
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    return module


class _FakeFacade:
    def __init__(self, status=None, report=None, control=None, raises=None):
        self._status, self._report, self._control = status, report, control
        self._raises = raises

    def handle_status_query(self, query):
        if self._raises == "status":
            raise RuntimeError("boom")
        return self._status

    def handle_report_query(self):
        if self._raises == "report":
            raise RuntimeError("boom")
        return self._report

    def handle_control_query(self, query):
        if self._raises == "control":
            raise RuntimeError("boom")
        return self._control


def _run(query, *, voice_facade, paperclip_calls, say_calls, media_calls):
    namespace = {
        "say": lambda text=None, *a, **k: say_calls.append(text),
        "paperclip_report": lambda q: paperclip_calls.append(q),
        "voice_facade": voice_facade,
        "_log": lambda *a, **k: None,
        # Legacy handler process_command falls through to on a "pausar..."
        # match with no orchestrator-specific hit - stands in for
        # media_key so finding #2's repro is real, not assumed.
        "media_key": lambda code: media_calls.append(code),
    }
    exec(compile(_extract_module(), str(_MAIN_PY), "exec"), namespace)
    return namespace["process_command"](query)


@pytest.fixture
def calls():
    return {"paperclip": [], "say": [], "media": []}


def _call(query, voice_facade, calls):
    return _run(
        query, voice_facade=voice_facade,
        paperclip_calls=calls["paperclip"], say_calls=calls["say"], media_calls=calls["media"],
    )


def test_report_phrase_reaches_the_facade_not_paperclip(calls):
    # Review Task #139, finding #1: _ORCHESTRATOR_REPORT_PHRASES are
    # supersets of _PAPERCLIP_TRIGGER_PHRASES ("relatório da orquestração"
    # contains "relatório") - the facade-specific intent must win.
    facade = _FakeFacade(report="relatorio real")
    result = _call("relatório da orquestração", facade, calls)

    assert result is True
    assert calls["say"] == ["relatorio real"]
    assert calls["paperclip"] == []


def test_status_phrase_reaches_the_facade_not_paperclip(calls):
    facade = _FakeFacade(status="status real")
    result = _call("como está a orquestração", facade, calls)

    assert result is True
    assert calls["say"] == ["status real"]
    assert calls["paperclip"] == []


def test_legacy_report_phrase_still_reaches_paperclip(calls):
    # The generic, pre-existing phrase (no "orquestração"/"orchestrator")
    # must still route to Paperclip - the reorder must not steal it.
    facade = _FakeFacade()
    result = _call("relatório", facade, calls)

    assert result is True
    assert calls["paperclip"] == ["relatório"]
    assert calls["say"] == []


def test_control_query_returning_none_never_falls_through_to_legacy_handler(calls):
    # Review Task #139, finding #2: a matched orchestrator intent that
    # comes back empty must be CONSUMED with a safe message, never fall
    # through to an unrelated legacy handler (media pause, here).
    facade = _FakeFacade(control=None)
    result = _call("pausar orquestração", facade, calls)

    assert result is True
    assert calls["media"] == []
    assert calls["say"] == ["A orquestração não está disponível agora, senhor."]


def test_control_query_raising_never_falls_through_to_legacy_handler(calls):
    facade = _FakeFacade(raises="control")
    result = _call("pausar orquestração", facade, calls)

    assert result is True
    assert calls["media"] == []
    assert calls["say"] == ["A orquestração não está disponível agora, senhor."]


def test_control_phrase_with_article_still_reaches_the_facade_not_media_pause(calls):
    # Real manual-test regression (issue #11): a user naturally says
    # "pausar A orquestração" - the article wasn't in the phrase list, so
    # this fell through past all three orchestrator blocks and matched
    # the legacy bare-"pausar" media-pause handler instead, reproducing
    # Review Task #139's original finding #2 for this exact phrasing.
    facade = _FakeFacade(control="Feito, senhor.")
    result = _call("pausar a orquestração", facade, calls)

    assert result is True
    assert calls["media"] == []
    assert calls["say"] == ["Feito, senhor."]


def test_retomar_control_phrase_with_article_still_reaches_the_facade(calls):
    facade = _FakeFacade(control="Feito, senhor.")
    result = _call("retomar a orquestração", facade, calls)

    assert result is True
    assert calls["media"] == []
    assert calls["say"] == ["Feito, senhor."]


def test_missing_facade_reports_unavailable_without_raising(calls):
    # Review Task #139, finding #3: `voice_facade is None` (the import
    # failed) must behave exactly like a runtime facade error.
    result = _call("status da orquestração", None, calls)

    assert result is True
    assert calls["say"] == ["A orquestração não está disponível agora, senhor."]
