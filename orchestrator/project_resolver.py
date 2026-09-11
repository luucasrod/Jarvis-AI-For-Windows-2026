"""Deterministic project-name resolver (issue #15).

Resolves what Lucas says ("Hub", "PDR", "Growth", ...) to the canonical
project entry in A:\\SecondBrain\\project_context_index.json - never by
guessing folder names (there are known near-homonym traps documented in
that index itself: Masya Studio/MASYA/Masya_Studio, Cashy vs Cashy-Android,
Argos vs Argos-Hub). 100% deterministic Python, no LLM involved (section
11/42) - this module only resolves METADATA, it does not load the content
of any always_read file (that is the planner's job, #22, on demand).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.config import load_config


@dataclass(frozen=True)
class ProjectContext:
    canonical_id: str
    root: str | None = None
    repository: str | None = None
    primary_context: str | None = None
    always_read: list[str] = field(default_factory=list)
    conditional_context: list[str] = field(default_factory=list)
    commands: dict = field(default_factory=dict)
    default_branch: str | None = None
    task_source: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolveError:
    reason: str
    suggestion: str | None = None


class ProjectResolver:
    """Caches the parsed index and reloads it if the file's mtime
    changes, so a long-running Jarvis process picks up SecondBrain index
    edits without a restart."""

    def __init__(self, index_path: str | None = None):
        self._index_path = Path(index_path or load_config().secondbrain_index_path)
        self._cached_mtime: float | None = None
        self._cached_data: dict | None = None
        self._cached_alias_map: dict[str, str] | None = None

    def _load_index(self) -> dict | ResolveError:
        try:
            mtime = os.path.getmtime(self._index_path)
        except OSError:
            return ResolveError(reason=f"indice nao encontrado em {self._index_path}")

        if self._cached_data is not None and self._cached_mtime == mtime:
            return self._cached_data

        try:
            raw = self._index_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except OSError as exc:
            return ResolveError(reason=f"erro ao ler indice: {exc}")
        except json.JSONDecodeError as exc:
            return ResolveError(reason=f"indice malformado (JSON invalido): {exc}")

        self._cached_data = data
        self._cached_mtime = mtime
        self._cached_alias_map = self._build_alias_map(data)
        return data

    @staticmethod
    def _build_alias_map(data: dict) -> dict[str, str]:
        alias_map: dict[str, str] = {}
        for canonical_id, aliases in (data.get("canonical_ids") or {}).items():
            alias_map[canonical_id.lower()] = canonical_id
            for alias in aliases or []:
                alias_map[str(alias).lower()] = canonical_id
        return alias_map

    def resolve(self, query: str) -> ProjectContext | ResolveError:
        data = self._load_index()
        if isinstance(data, ResolveError):
            return data

        alias_map = self._cached_alias_map or {}
        canonical_id = alias_map.get(query.strip().lower())
        if canonical_id is None:
            suggestion = self._closest_alias(query, alias_map.keys())
            return ResolveError(
                reason=f"projeto '{query}' nao encontrado no indice",
                suggestion=suggestion,
            )

        project = (data.get("projects") or {}).get(canonical_id)
        if project is None:
            return ResolveError(
                reason=f"'{canonical_id}' esta em canonical_ids mas nao em projects - indice inconsistente"
            )

        return ProjectContext(
            canonical_id=canonical_id,
            root=project.get("root"),
            repository=project.get("repository") or project.get("repository_owner_repo"),
            primary_context=project.get("primary_context"),
            always_read=list(project.get("always_read") or []),
            conditional_context=list(project.get("conditional_context") or []),
            commands=dict(project.get("commands") or {}),
            default_branch=project.get("default_branch_seen"),
            task_source=project.get("task_source"),
            warnings=list(project.get("warnings") or []),
        )

    @staticmethod
    def _closest_alias(query: str, candidates) -> str | None:
        """Very small, dependency-free 'did you mean' - substring match
        only, not a full fuzzy-match algorithm (out of scope)."""
        q = query.strip().lower()
        for candidate in candidates:
            if q in candidate or candidate in q:
                return candidate
        return None


def resolve(query: str, index_path: str | None = None) -> ProjectContext | None:
    """Convenience module-level function matching the issue's scope
    signature. Returns None on any failure (see ResolveError) - callers
    needing the failure reason should use ProjectResolver directly."""
    result = ProjectResolver(index_path).resolve(query)
    return result if isinstance(result, ProjectContext) else None
