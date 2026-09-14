"""Deploy strategy discovery + post-deploy smoke check (issue #38).

Never duplicates an existing auto-deploy pipeline (section 33/34/35): this
module only DISCOVERS what a project's own deploy already does (read from
#15's ProjectContext) and reacts to it after the fact with a proportional
smoke check - it never triggers a deploy itself (out of scope).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

import requests

from orchestrator.decisions import notify_needs_lucas
from orchestrator.github_client import GitHubClient
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store

_NOT_APPLICABLE_MARKERS = ("not applicable", "nao aplicavel", "n/a", "none")
_UNRESOLVED_MARKER = "unresolved"
_AUTO_TRIGGER_MARKERS = ("automatic", "automatico", "auto ")
_MOBILE_COMMAND_KEYS = ("android", "ios")


@dataclass(frozen=True)
class DeployStrategy:
    """What (if anything) a project's OWN deploy pipeline already does -
    discovered, never invented. `kind` decides which smoke check
    `run_smoke_check` runs; `trigger`/`provider`/`production_url` are
    informational."""
    kind: str  # "web" | "api" | "app" | "none" | "unknown"
    trigger: str  # "auto_on_push" | "manual" | "none" | "unknown"
    provider: str | None = None
    production_url: str | None = None
    notes: str | None = None


def _is_unset(text: str) -> bool:
    lowered = text.strip().lower()
    return not lowered or lowered.startswith(_UNRESOLVED_MARKER) or any(
        marker in lowered for marker in _NOT_APPLICABLE_MARKERS
    )


def get_deploy_strategy(project_context) -> DeployStrategy:
    """Reads the deploy strategy already documented for this project in
    the SecondBrain index (#15) - never guesses, never invents a new
    strategy the project doesn't already have.

    Two sources, most-specific first: a structured `deploy` object (only
    some index entries have one - provider/production_url/trigger, added
    by #38 to ProjectContext) takes priority when present, since it
    carries a real `production_url` to actually check. Otherwise falls
    back to the freeform `commands["deploy"]` string every project has.
    A project with mobile build commands (`android`/`ios`) is classified
    "app" regardless of the deploy text - its smoke check is a build
    proxy, not an HTTP call (see `run_smoke_check`).
    """
    commands = project_context.commands or {}
    is_mobile = any(key in commands for key in _MOBILE_COMMAND_KEYS)

    structured = project_context.deploy or {}
    if structured.get("production_url"):
        trigger_text = str(structured.get("trigger") or "").strip().lower()
        trigger = "auto_on_push" if any(m in trigger_text for m in _AUTO_TRIGGER_MARKERS) else (
            "manual" if trigger_text else "unknown"
        )
        return DeployStrategy(
            kind="app" if is_mobile else "web",
            trigger=trigger,
            provider=structured.get("provider"),
            production_url=structured.get("production_url"),
            notes=trigger_text or None,
        )

    deploy_text = str(commands.get("deploy") or "")
    if is_mobile:
        return DeployStrategy(kind="app", trigger="unknown", notes=deploy_text or None)
    if _is_unset(deploy_text):
        return DeployStrategy(kind="none", trigger="none", notes=deploy_text or None)

    lowered = deploy_text.lower()
    trigger = "auto_on_push" if any(m in lowered for m in _AUTO_TRIGGER_MARKERS) else "manual"
    provider = "Vercel" if "vercel" in lowered else None
    # No production_url available from freeform text alone - a project
    # only gets an HTTP smoke check when the structured `deploy` object
    # (above) actually supplies one; otherwise it's classified "unknown"
    # so run_smoke_check skips rather than guessing at an endpoint.
    kind = "web" if provider else "unknown"
    return DeployStrategy(kind=kind, trigger=trigger, provider=provider, notes=deploy_text)


@dataclass(frozen=True)
class SmokeCheckResult:
    ok: bool
    kind: str
    detail: str
    expected: str | None = None
    observed: str | None = None
    emergency: bool = False


def run_smoke_check(
    project_context, strategy: DeployStrategy, *,
    get_fn: Callable | None = None, timeout: float = 10.0,
) -> SmokeCheckResult:
    """Runs a check PROPORTIONAL to `strategy.kind` (section 34 - no
    giant E2E suite here):
    - "web"/"api": one HTTP GET to `strategy.production_url`, expects a
      non-5xx, non-network-failure response.
    - "app": confirms a real build command is configured (not
      UNRESOLVED/none) - never actually runs a build or starts an
      emulator, which section 34 explicitly rules out as disproportionate
      for a routine check.
    - "none"/"unknown": nothing to check - returns ok=True rather than
      manufacturing a finding from missing information.

    `emergency=True` only when the target could not be reached AT ALL
    (connection refused/timeout) - "producao caiu" is the one signal a
    plain HTTP smoke check can detect with real confidence; a reachable
    server returning an error status is a normal bug (BUG_FOUND), not an
    automatic escalation (issue #38's own scope: don't stop the
    organization over a non-catastrophic failure).
    """
    if strategy.kind in ("web", "api"):
        if not strategy.production_url:
            return SmokeCheckResult(
                ok=True, kind=strategy.kind,
                detail="sem production_url conhecida - nada para verificar via HTTP",
            )
        get = get_fn or requests.get
        try:
            response = get(strategy.production_url, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            return SmokeCheckResult(
                ok=False, kind=strategy.kind,
                detail=f"nao foi possivel alcancar {strategy.production_url}",
                expected="resposta HTTP (qualquer status < 500)",
                observed=f"falha de conexao: {exc}",
                emergency=True,
            )
        status = getattr(response, "status_code", None)
        if status is None or status >= 500:
            return SmokeCheckResult(
                ok=False, kind=strategy.kind,
                detail=f"{strategy.production_url} respondeu com erro de servidor",
                expected="status HTTP < 500",
                observed=f"status {status}",
            )
        return SmokeCheckResult(
            ok=True, kind=strategy.kind,
            detail=f"{strategy.production_url} respondeu {status}",
        )

    if strategy.kind == "app":
        build_command = str((project_context.commands or {}).get("build") or "")
        if _is_unset(build_command):
            return SmokeCheckResult(
                ok=False, kind="app",
                detail="nenhum comando de build configurado para o projeto",
                expected="commands.build definido e resolvido",
                observed=build_command or "(ausente)",
            )
        return SmokeCheckResult(
            ok=True, kind="app",
            detail=f"comando de build confirmado: {build_command}",
        )

    return SmokeCheckResult(
        ok=True, kind=strategy.kind,
        detail=f"estrategia de deploy '{strategy.kind}' - nada para verificar",
    )


def _correlation_id(project_context, result: SmokeCheckResult) -> str:
    """Deterministic per (project, kind, failure detail) - the SAME
    recurring failure across daily cycles dedupes to one Issue via #17's
    own correlation_id mechanism, instead of spamming a new one every
    time the same problem is still unresolved."""
    payload = json.dumps(
        [project_context.canonical_id, result.kind, result.detail], sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _format_bug_body(project_context, result: SmokeCheckResult) -> str:
    lines = [
        f"Projeto: {project_context.canonical_id}",
        f"Branch: {project_context.default_branch or 'desconhecida'}",
        "",
        "## ESPERADO",
        result.expected or "(nao especificado)",
        "",
        "## OBSERVADO",
        result.observed or result.detail,
        "",
        "## REPRODUCAO",
        f"Smoke check automatico pos-deploy ({result.kind}).",
        "",
        "## EVIDENCIA",
        result.detail,
        "",
        "## AREA PROVAVEL",
        result.kind,
        "",
        "## SEVERIDADE",
        "emergencia (producao inacessivel)" if result.emergency else "normal",
    ]
    return "\n".join(lines)


def report_smoke_check_failure(
    project_context, result: SmokeCheckResult, store: Store, *,
    client: GitHubClient | None = None,
) -> Task:
    """Records a failed smoke check as a real, durable Task (state
    BUG_FOUND, or NEEDS_LUCAS for a genuine emergency - see
    `run_smoke_check`'s own emergency rule), creates the matching GitHub
    Issue (idempotent per #17's correlation_id dedup - a still-unresolved
    failure across cycles never spams a duplicate Issue), and only pages
    Lucas (#33's NEEDS_LUCAS flow) for that emergency case. A normal
    BUG_FOUND never escalates - per #38's own scope, it must never stop
    the organization."""
    correlation_id = _correlation_id(project_context, result)
    task = Task(
        title=f"[smoke check] {project_context.canonical_id}: {result.detail}",
        objective=result.detail,
        project_id=project_context.canonical_id,
        origin="post_deploy_check",
        state=TaskState.NEEDS_LUCAS if result.emergency else TaskState.BUG_FOUND,
        correlation_id=correlation_id,
    )
    store.save_task(task)

    if client is not None and project_context.repository:
        client.create_issue(
            project_context.repository, task.title, _format_bug_body(project_context, result),
            ["origin:post_deploy_check"], correlation_id,
        )

    if result.emergency:
        notify_needs_lucas(
            task,
            problem=f"{project_context.canonical_id} parece fora do ar apos o deploy.",
            why="Smoke check pos-deploy nao conseguiu alcancar a producao - possivel queda total.",
            options=["Investigar agora", "Aguardar e reverificar", "Reverter o ultimo deploy"],
            recommendation="Investigar agora - producao inacessivel bloqueia todos os usuarios.",
            impact="Producao pode estar totalmente indisponivel para os usuarios enquanto isso.",
            store=store,
        )

    return task
