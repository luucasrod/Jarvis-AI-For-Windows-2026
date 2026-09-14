"""Facade between the voice dispatcher (main.py) and orchestrator/ (#11).

The SINGLE contact point between the ~2000-line main.py monolith and this
package (PROMPT MESTRE V2 section 6/56) - main.py never imports any other
orchestrator module directly for voice. Every function here is a stub for
now: it always returns a clear "under construction" answer rather than
real orchestration data. The real implementations land with #27 (Wave 4),
which reuses these exact same signatures - main.py's own call sites are
not expected to change when that happens.
"""
from __future__ import annotations

_NOT_IMPLEMENTED = (
    "Isso ainda esta em construcao, senhor - a orquestracao completa do Jarvis "
    "ainda nao esta ligada a voz."
)


def handle_status_query(query: str) -> str | None:
    """Returns a spoken answer for a status question (e.g. "como esta o
    projeto X"), or `None` if `query` isn't one this facade recognizes -
    letting the caller fall through to its next handler. Stub: always
    answers "under construction" once the caller has already matched an
    intent and calls this."""
    return _NOT_IMPLEMENTED


def handle_report_query() -> str:
    """Returns the spoken daily-report text. Unlike the other two, this
    is unconditional - there is no "not a report question" case once the
    caller has matched the report intent. Stub: always "under
    construction"."""
    return _NOT_IMPLEMENTED


def handle_control_query(query: str) -> str | None:
    """Returns a spoken acknowledgement for a control command (e.g.
    "pausar a orquestracao"), or `None` if `query` isn't one this facade
    recognizes. Stub: always answers "under construction" once the caller
    has already matched an intent and calls this."""
    return _NOT_IMPLEMENTED
