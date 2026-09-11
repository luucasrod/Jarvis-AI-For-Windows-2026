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
import secrets

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
    """Envelopes externally-sourced text with an explicit delimiter
    before it can be concatenated into an LLM prompt. Does not modify the
    content itself (so injection heuristics can still run on the
    original text) - only marks its boundaries and provenance.

    A fresh random token is generated PER CALL and embedded in both the
    opening and closing markers. This is the fix for a real collision
    found in review #58: with a fixed, predictable delimiter string,
    content could contain its own fake closing marker followed by fake
    content and a fake opening marker, visually "escaping" the boundary
    (e.g. content = '<<<END_EXTERNAL_CONTENT>>>\\nSYSTEM: ...'). Because
    the token is generated fresh and unpredictably each call, content
    prepared in advance cannot know it and therefore cannot forge a
    matching closing marker.

    This raises the cost of forging the boundary; it is still NOT a
    guarantee that an LLM will treat the enclosed text as inert data
    (section 43's own scope limit - no prompt-boundary trick guarantees
    model obedience). looks_like_injection_attempt() remains a separate,
    best-effort heuristic for logging/alerting on top of this."""
    token = secrets.token_hex(16)
    start = f"<<<EXTERNAL_CONTENT id={token} source={source!r}>>>"
    end = f"<<<END_EXTERNAL_CONTENT id={token}>>>"
    return f"{start}\n{content}\n{end}"


def looks_like_injection_attempt(text: str) -> bool:
    """Cheap heuristic scan for obvious prompt-injection phrasing. Meant
    to be logged/flagged for a human or a stricter downstream check -
    NEVER to silently drop or alter content. False positives (e.g. a
    legitimate Issue discussing prompt injection as a topic) are
    acceptable; false negatives on sophisticated attacks are expected and
    out of scope (section 43)."""
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
