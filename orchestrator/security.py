"""Prompt-injection boundary between POLICY and EXTERNAL CONTENT (issue #21).

RULE FOR EVERY FUTURE MODULE THAT TALKS TO AN LLM (Gemini/Groq): any text
that did not originate from Jarvis's own system prompt/policy - an Issue
body, a README, a log line, an HTTP response body, a comment - is DATA,
never an instruction. Before that text is concatenated into any prompt,
it MUST go through `wrap_external_content()`. The planner (#22) is the
first and most important consumer of this rule.

This is defense-in-depth, not a complete solution (PROMPT MESTRE V2
section 43 explicitly scopes out a robust ML classifier here).
`looks_like_injection_attempt` is a cheap heuristic meant for logging/
alerting - false positives are fine, it must NEVER be used to silently
block content outright (that would need human judgement, not this
module).
"""
from __future__ import annotations

import re

_BOUNDARY_START = "<<<EXTERNAL_CONTENT source={source!r}>>>"
_BOUNDARY_END = "<<<END_EXTERNAL_CONTENT>>>"

_INJECTION_PATTERNS = [
    re.compile(r"ignore (all |any )?(previous|prior|above) instructions", re.IGNORECASE),
    re.compile(r"disregard (all |any )?(previous|prior|above)", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*assistant\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"you are now", re.IGNORECASE),
    re.compile(r"new instructions?:", re.IGNORECASE),
    re.compile(r"reveal (your|the) (system )?prompt", re.IGNORECASE),
    re.compile(r"act as (if you|though)", re.IGNORECASE),
]


def wrap_external_content(source: str, content: str) -> str:
    """Envelopes externally-sourced text with an explicit, hard-to-forge
    delimiter before it can be concatenated into an LLM prompt. Does not
    modify the content itself (so injection heuristics can still run on
    the original text) - only marks its boundaries and provenance."""
    start = _BOUNDARY_START.format(source=source)
    return f"{start}\n{content}\n{_BOUNDARY_END}"


def looks_like_injection_attempt(text: str) -> bool:
    """Cheap heuristic scan for obvious prompt-injection phrasing. Meant
    to be logged/flagged for a human or a stricter downstream check -
    NEVER to silently drop or alter content. False positives (e.g. a
    legitimate Issue discussing prompt injection as a topic) are
    acceptable; false negatives on sophisticated attacks are expected and
    out of scope (section 43)."""
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
