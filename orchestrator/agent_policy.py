"""Agent classification and cross-review policy (issue #24).

Decides, for a given Task, its AgentClass, ExecutionMode, preferred/
fallback agent and reviewer preference - per PROMPT MESTRE V2 sections
20/54/55/66/67:

- Architecture/security/decision-heavy work -> Claude preferred.
- Mechanical/CRUD/wrapper/pattern-extension work -> Codex preferred.
- Genuinely either -> FLEX, load-balanced by current_load (never a forced
  50/50, never an artificial imbalance either).
- SOLO is an EXECUTION property (file/area conflict risk - e.g. anything
  touching main.py), independent of agent_class (section 67): a task can
  be FLEX + SOLO + preferred Claude at the same time.
- Reviewer preference is always the OTHER agent from preferred_agent
  (cross-review, section 24) - never the implementer reviewing itself.

This module only produces the INITIAL classification. Dynamic
reassignment when an agent hits a rate limit is #26's job (I19) - this
issue explicitly scopes that out.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task

# Keyword heuristics are intentionally simple substring/word checks
# (section 42: deterministic Python, no LLM needed for this). Architecture
# keywords win on overlap with mechanical ones - a task that is both "add
# a client wrapper" and "design the security boundary" is judged by its
# hardest part.
_ARCHITECTURE_KEYWORDS = [
    "arquitetura", "architecture", "planner", "planejador", "seguranca",
    "security", "policy", "politica", "decisao", "design", "modelo de dados",
    "pipeline de revisao", "escalonamento", "balanceamento",
]
_MECHANICAL_KEYWORDS = [
    "cliente", "client", "wrapper", "crud", "endpoint", "parser",
    "extensao", "docs", "documentacao", "documentation", "log",
    "scaffold", "esqueleto", "config", "migracao", "script", "healthcheck",
]

_HOTSPOT_PATTERN = re.compile(r"\bmain\.py\b", re.IGNORECASE)

# Minimal, documented contract for project-specific hotspots (found
# missing in review #64): `project_context` may optionally declare a
# `hotspots` list of filenames/substrings - either as an attribute (a
# dataclass/object) or a "hotspots" key (a plain dict). Nothing in the
# real SecondBrain index or orchestrator.project_resolver.ProjectContext
# (#15) defines this today; this is deliberately permissive so either
# can add it later without a breaking change here.
#   project_context.hotspots = ["shared.py", "config.py"]
#   project_context = {"hotspots": ["shared.py"]}


@dataclass(frozen=True)
class AgentAssignment:
    agent_class: AgentClass
    execution_mode: ExecutionMode
    preferred_agent: AgentName
    fallback_agent: AgentName
    reviewer_preference: AgentName


def _strip_accents(text: str) -> str:
    """Normalizes accented PT-BR/PT-PT characters (Seguranca vs
    Segurança) so keyword matching doesn't depend on the caller's
    spelling. Found in review #64: keyword lists are accent-free ASCII,
    but real task titles/objectives are normal Portuguese prose."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _matches_any(text: str, keywords: list[str]) -> bool:
    text_normalized = _strip_accents(text).lower()
    return any(keyword in text_normalized for keyword in keywords)


def _classify_base(task: Task) -> AgentClass:
    combined = f"{task.title} {task.objective} {task.context}"
    is_architecture = _matches_any(combined, _ARCHITECTURE_KEYWORDS)
    is_mechanical = _matches_any(combined, _MECHANICAL_KEYWORDS)

    if is_architecture:
        return AgentClass.CLAUDE
    if is_mechanical:
        return AgentClass.CODEX
    return AgentClass.FLEX


def _extract_hotspots(project_context) -> list[str]:
    if project_context is None:
        hotspots = None
    elif isinstance(project_context, dict):
        hotspots = project_context.get("hotspots")
    else:
        hotspots = getattr(project_context, "hotspots", None)
    return [str(h) for h in hotspots] if isinstance(hotspots, list) else []


def _is_hotspot(task: Task, project_context=None) -> bool:
    combined = f"{task.title} {task.objective} {task.context} {task.probable_area or ''}"
    if _HOTSPOT_PATTERN.search(combined):
        return True

    for hotspot in _extract_hotspots(project_context):
        pattern = re.compile(r"\b" + re.escape(hotspot) + r"\b", re.IGNORECASE)
        if pattern.search(combined):
            return True
    return False


def _pick_flex_preferred(current_load: dict[str, int] | None) -> AgentName:
    load = current_load or {}
    claude_load = load.get("Claude", 0)
    codex_load = load.get("Codex", 0)
    if claude_load < codex_load:
        return AgentName.CLAUDE
    if codex_load < claude_load:
        return AgentName.CODEX
    return AgentName.EITHER


def _opposite(agent: AgentName) -> AgentName:
    if agent == AgentName.CLAUDE:
        return AgentName.CODEX
    if agent == AgentName.CODEX:
        return AgentName.CLAUDE
    return AgentName.EITHER


def classify_task(
    task: Task,
    project_context=None,
    current_load: dict[str, int] | None = None,
) -> AgentAssignment:
    """
    Note (raised in review #64): when a FLEX task's current_load is tied,
    both preferred_agent and reviewer_preference come back as
    AgentName.EITHER. That is a genuinely unresolved assignment, not a
    real guarantee of cross-review - whatever dispatches the task (the
    orchestration wiring, #23) MUST pick one concrete implementer and the
    opposite concrete reviewer before execution starts. This function
    does not do that pick itself (it has no notion of which agent is
    actually available right now) and dynamic reassignment on rate limit
    is explicitly #26's job, not this one's.
    """
    agent_class = _classify_base(task)
    execution_mode = ExecutionMode.SOLO if _is_hotspot(task, project_context) else task.execution_mode

    if agent_class == AgentClass.CLAUDE:
        preferred = AgentName.CLAUDE
        fallback = AgentName.CODEX
    elif agent_class == AgentClass.CODEX:
        preferred = AgentName.CODEX
        fallback = AgentName.CLAUDE
    else:  # FLEX
        preferred = _pick_flex_preferred(current_load)
        fallback = AgentName.NONE if preferred == AgentName.EITHER else _opposite(preferred)

    reviewer = _opposite(preferred)

    return AgentAssignment(
        agent_class=agent_class,
        execution_mode=execution_mode,
        preferred_agent=preferred,
        fallback_agent=fallback,
        reviewer_preference=reviewer,
    )
