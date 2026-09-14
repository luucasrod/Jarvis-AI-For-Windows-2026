"""Deterministic project-name resolver (issue #15).

Resolves what Lucas says ("Hub", "PDR", "Growth", ...) to the canonical
project entry in A:\\SecondBrain\\project_context_index.json - never by
guessing folder names (there are known near-homonym traps documented in
that index itself: Masya Studio/MASYA/Masya_Studio, Cashy vs Cashy-Android,
Argos vs Argos-Hub). 100% deterministic Python, no LLM involved (section
11/42) - this module only resolves METADATA, it does not load the content
of any always_read file (that is the planner's job, #22, on demand).

`resolve_from_text()` was added in #22 (planner) to identify which known
project an objective sentence refers to - additive, does not change
`resolve()`'s existing behavior from #15.
"""
from __future__ import annotations

import json
import os
import re
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
    deploy: dict = field(default_factory=dict)


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
        except UnicodeDecodeError as exc:
            return ResolveError(reason=f"indice nao esta em UTF-8 valido: {exc}")
        except json.JSONDecodeError as exc:
            return ResolveError(reason=f"indice malformado (JSON invalido): {exc}")

        # Validate STRUCTURE before caching anything - an index that
        # parses as JSON but has the wrong shape (list instead of dict,
        # canonical_ids as a list instead of a dict, a project entry that
        # isn't a dict, etc) must never get cached, so a later corrected
        # file is picked up on the very next call instead of staying
        # stuck behind a bad cache (found in review #60: previously
        # cached_data/mtime were updated BEFORE building the alias map,
        # so a structurally-invalid index could poison the cache).
        error = self._validate_structure(data)
        if error is not None:
            return error

        self._cached_data = data
        self._cached_mtime = mtime
        self._cached_alias_map = self._build_alias_map(data)
        return data

    @staticmethod
    def _validate_structure(data) -> ResolveError | None:
        if not isinstance(data, dict):
            return ResolveError(reason=f"indice invalido: esperava um objeto JSON, recebeu {type(data).__name__}")

        canonical_ids = data.get("canonical_ids", {})
        if not isinstance(canonical_ids, dict):
            return ResolveError(reason="indice invalido: 'canonical_ids' deveria ser um objeto (dict)")
        for canonical_id, aliases in canonical_ids.items():
            if aliases is not None and not isinstance(aliases, list):
                return ResolveError(
                    reason=f"indice invalido: aliases de '{canonical_id}' deveriam ser uma lista"
                )

        projects = data.get("projects", {})
        if not isinstance(projects, dict):
            return ResolveError(reason="indice invalido: 'projects' deveria ser um objeto (dict)")
        for canonical_id, project in projects.items():
            if project is not None and not isinstance(project, dict):
                return ResolveError(
                    reason=f"indice invalido: entrada de projeto '{canonical_id}' deveria ser um objeto (dict)"
                )
            if not isinstance(project, dict):
                continue

            # Found by review #60 (2nd pass): _build_context wraps these
            # 4 fields in list()/dict() unconditionally - a scalar value
            # here (e.g. `"commands": 42`) crashed with TypeError instead
            # of returning a structured error, even though the top-level
            # shape checks above already passed.
            for list_field in ("always_read", "conditional_context", "warnings"):
                value = project.get(list_field)
                if value is not None and not isinstance(value, list):
                    return ResolveError(
                        reason=(
                            f"indice invalido: '{list_field}' do projeto '{canonical_id}' "
                            "deveria ser uma lista"
                        )
                    )
            commands = project.get("commands")
            if commands is not None and not isinstance(commands, dict):
                return ResolveError(
                    reason=f"indice invalido: 'commands' do projeto '{canonical_id}' deveria ser um objeto (dict)"
                )
            deploy = project.get("deploy")
            if deploy is not None and not isinstance(deploy, dict):
                return ResolveError(
                    reason=f"indice invalido: 'deploy' do projeto '{canonical_id}' deveria ser um objeto (dict)"
                )

        return None

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

        return self._build_context(canonical_id, data)

    def resolve_from_text(self, text: str) -> ProjectContext | ResolveError:
        """Scans free-form text (e.g. a planner objective sentence, not
        just a bare project name) for the longest matching known alias,
        as a whole word/phrase (not a substring inside an unrelated
        word - e.g. 'HA' must not match inside 'have'). Deterministic,
        no LLM involved (section 11/42 of PROMPT MESTRE V2): identifying
        which known project a sentence refers to is treated as a lookup
        problem, not a semantic one, given the index already enumerates
        every alias explicitly."""
        data = self._load_index()
        if isinstance(data, ResolveError):
            return data

        alias_map = self._cached_alias_map or {}
        text_lower = text.lower()

        best_alias: str | None = None
        for alias in alias_map:
            pattern = r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])"
            if re.search(pattern, text_lower):
                if best_alias is None or len(alias) > len(best_alias):
                    best_alias = alias

        if best_alias is None:
            return ResolveError(reason=f"nenhum projeto conhecido mencionado em: {text!r}")

        return self._build_context(alias_map[best_alias], data)

    def _build_context(self, canonical_id: str, data: dict) -> ProjectContext | ResolveError:
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
            # Additive (#38): a structured "deploy" object (provider,
            # production_url, trigger) is only present for SOME index
            # entries - the freeform commands["deploy"] string above
            # remains the universal fallback every project has.
            deploy=dict(project.get("deploy") or {}),
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
