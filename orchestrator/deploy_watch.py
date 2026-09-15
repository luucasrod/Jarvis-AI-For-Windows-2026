"""Deploy strategy discovery + post-deploy smoke check (issue #38).

Never duplicates an existing auto-deploy pipeline (section 33/34/35): this
module only DISCOVERS what a project's own deploy already does (read from
#15's ProjectContext) and reacts to it after the fact with a proportional
smoke check - it never triggers a deploy itself (out of scope).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from orchestrator.decisions import notify_needs_lucas
from orchestrator.events import EventType, emit_in_transaction
from orchestrator.github_client import GitHubClient
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store

_NOT_APPLICABLE_MARKERS = ("not applicable", "nao aplicavel", "n/a", "none")
_UNRESOLVED_MARKER = "unresolved"
_AUTO_TRIGGER_MARKERS = (
    "automatic", "automatico", "auto-deploy", "autodeploy", "auto deploy",
    "auto_on_push", "ci/cd", "continuous deployment", "on push", "on every push",
)
_MOBILE_COMMAND_KEYS = ("android", "ios")
_VALID_KINDS = ("web", "api", "app")
_CLOSED_TASK_STATES = (TaskState.DONE.value, TaskState.FAILED.value)

# issue_title/issue_body persist the exact strings used for the episode's
# GitHub Issue at creation/adoption time (round-2 finding #2): GitHubClient's
# own idempotency compares the FULL payload for a given correlation_id and
# refuses ('correlation_conflict') on any mismatch, so a later call for the
# same still-open episode must replay these verbatim, never recompute them
# from that call's own commit/logs.
_EPISODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS deploy_failure_episodes (
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_hash TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    issue_title TEXT,
    issue_body TEXT,
    PRIMARY KEY (project_id, kind, detail_hash)
);
"""


def _sanitize_url(url: str) -> str:
    """Strips credentials and the query string before a production URL is
    interpolated into any persisted/published text - round-2 finding #3:
    `https://user:pass@host/health?token=SECRET` leaked the password and
    token straight into the Task title, Issue title and Issue body. Only
    scheme+host+path are safe to publish; the real URL (with credentials/
    query intact) is used solely at the HTTP boundary in `run_smoke_check`'s
    own `get(...)` call, never anywhere else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(url invalida)"
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _is_unset(text: str) -> bool:
    lowered = text.strip().lower()
    return not lowered or lowered.startswith(_UNRESOLVED_MARKER) or any(
        marker in lowered for marker in _NOT_APPLICABLE_MARKERS
    )


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


def get_deploy_strategy(project_context) -> DeployStrategy:
    """Reads the deploy strategy already documented for this project in
    the SecondBrain index (#15) - never guesses, never invents a new
    strategy the project doesn't already have.

    Two sources, most-specific first: a structured `deploy` object (only
    some index entries have one - provider/production_url/trigger, added
    by #38 to ProjectContext) takes priority when present, since it
    carries a real `production_url` to actually check. An optional
    `deploy["kind"]` override (one of "web"/"api"/"app") lets a FUTURE
    index entry state its real kind explicitly - the index currently has
    no field distinguishing a web app from an API at all, so without this
    override "api" is unreachable from real data (Review Task #143
    finding #6); this doesn't invent that data, it just makes room for it
    once the index has it. Absent an override, "web" remains the default
    assumption for any URL-based check. Falls back to the freeform
    `commands["deploy"]` string every project has when no structured
    object exists. A project with mobile build commands (`android`/`ios`)
    is classified "app" regardless of the deploy text - its smoke check
    is a build-evidence check, not an HTTP call (see `run_smoke_check`).
    """
    commands = project_context.commands or {}
    is_mobile = any(key in commands for key in _MOBILE_COMMAND_KEYS)

    structured = project_context.deploy or {}
    if structured.get("production_url"):
        trigger_text = str(structured.get("trigger") or "").strip().lower()
        trigger = "auto_on_push" if any(m in trigger_text for m in _AUTO_TRIGGER_MARKERS) else (
            "manual" if trigger_text else "unknown"
        )
        override = structured.get("kind")
        kind = override if override in _VALID_KINDS else ("app" if is_mobile else "web")
        return DeployStrategy(
            kind=kind, trigger=trigger, provider=structured.get("provider"),
            production_url=structured.get("production_url"), notes=trigger_text or None,
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
    verified: bool = True
    expected: str | None = None
    observed: str | None = None
    emergency: bool = False


def _safe_observed(exc: Exception) -> str:
    """Never includes str(exc) - `requests` exceptions routinely embed the
    full request URL (query params, and any header echoed back in a
    redirect/auth error), which can carry a token or secret (Review Task
    #143 finding #4: a ConnectionError's own text leaked an
    Authorization header straight into a public GitHub Issue body).
    Only the exception's TYPE name is safe to surface."""
    return f"falha de rede/requisicao ({type(exc).__name__})"


def run_smoke_check(
    project_context, strategy: DeployStrategy, *,
    get_fn: Callable | None = None, timeout: float = 10.0,
    expected_status: Callable[[int], bool] | None = None,
    build_result: bool | None = None,
) -> SmokeCheckResult:
    """Runs a check PROPORTIONAL to `strategy.kind` (section 34 - no
    giant E2E suite here):

    - "web"/"api": one HTTP GET to `strategy.production_url`.
      `expected_status` (default: 200-399) decides pass/fail - ANY
      status outside that range fails, including 4xx (Review Task #143
      finding #3: a 404 from a broken health endpoint used to pass
      because only >=500 counted as failure). Only a genuine
      connectivity failure (`ConnectionError`/`Timeout` - the request
      never got a response at all) is `emergency=True`; every other
      `RequestException` (bad URL, too many redirects, ... - a LOCAL
      configuration mistake, not proof production is down) is a normal,
      non-emergency failure (finding #4). No exception text is ever
      surfaced (see `_safe_observed`).
    - "app": if the caller supplies real evidence via `build_result`
      (from wherever the actual build/test already ran - #38 itself
      never runs one), that evidence decides pass/fail and `verified` is
      True. Without it, this falls back to a WEAK proxy (a real,
      resolved `commands["build"]` exists) and is explicitly marked
      `verified=False` - it is evidence a build COULD run, never proof
      one succeeded (finding #3: `commands.build = "exit 1"` used to
      pass this check with no evidence at all).
    - "none"/"unknown"/no `production_url`: nothing to check -
      `ok=True, verified=False`, never manufactures a finding from
      missing information.
    """
    if strategy.kind in ("web", "api"):
        if not strategy.production_url:
            return SmokeCheckResult(
                ok=True, kind=strategy.kind, verified=False,
                detail="sem production_url conhecida - nada para verificar via HTTP",
            )
        get = get_fn or requests.get
        is_ok_status = expected_status or (lambda code: 200 <= code < 400)
        try:
            response = get(strategy.production_url, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            return SmokeCheckResult(
                ok=False, kind=strategy.kind,
                detail="alvo de producao inalcancavel - suspeita de queda (uma unica sondagem, nao confirmada)",
                expected="conexao HTTP bem-sucedida",
                observed=_safe_observed(exc),
                emergency=True,
            )
        except requests.exceptions.RequestException as exc:
            # A configuration/local error (bad URL, redirect loop, ...) -
            # never proof production is down, never worth paging Lucas.
            return SmokeCheckResult(
                ok=False, kind=strategy.kind,
                detail="requisicao de smoke check falhou por erro de configuracao/local",
                expected="requisicao HTTP valida",
                observed=_safe_observed(exc),
            )
        safe_url = _sanitize_url(strategy.production_url)
        status = getattr(response, "status_code", None)
        if status is None or not is_ok_status(status):
            return SmokeCheckResult(
                ok=False, kind=strategy.kind,
                detail=f"{safe_url} respondeu com status inesperado ({status})",
                expected="status HTTP aceito pelo criterio configurado",
                observed=f"status {status}",
            )
        return SmokeCheckResult(
            ok=True, kind=strategy.kind,
            detail=f"{safe_url} respondeu {status}",
        )

    if strategy.kind == "app":
        if build_result is not None:
            return SmokeCheckResult(
                ok=build_result, kind="app",
                detail="resultado de build/teste real injetado pelo chamador",
                observed=str(build_result),
            )
        build_command = str((project_context.commands or {}).get("build") or "")
        if _is_unset(build_command):
            return SmokeCheckResult(
                ok=False, kind="app", verified=False,
                detail="nenhum comando de build configurado para o projeto",
                expected="commands.build definido e resolvido",
                observed=build_command or "(ausente)",
            )
        return SmokeCheckResult(
            ok=True, kind="app", verified=False,
            detail=f"comando de build existe ({build_command}) - nao executado, sem evidencia real",
        )

    return SmokeCheckResult(
        ok=True, kind=strategy.kind, verified=False,
        detail=f"estrategia de deploy '{strategy.kind}' - nada para verificar",
    )


def _detail_hash(project_context, result: SmokeCheckResult) -> str:
    payload = json.dumps([project_context.canonical_id, result.kind, result.detail], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reserve_episode(
    store: Store, project_id: str, kind: str, detail_hash: str, task: Task,
    issue_title: str, issue_body: str,
) -> tuple[str, Task, bool, str, str]:
    """Atomically reserves the identity for this failure EPISODE - the
    first and only place a Task gets minted for it, and the only place
    BUG_FOUND is emitted for a new one - closing Review Task #143
    findings #1, #2 and #4 together. Returns
    (episode_id, task, is_new_episode, issue_title, issue_body) - the
    last two are the strings the CALLER must send to GitHubClient
    (possibly not the ones it passed in - see finding #2 below).

    Finding #1 (idempotency only at the GitHub layer): every call used to
    build a fresh Task with a random UUID, so two identical failures
    across cycles created two local Tasks (and, since NEEDS_LUCAS's own
    decision correlation includes task.id, two separate pages to Lucas
    too). An OPEN episode (same project/kind/detail, not yet resolved)
    now always reuses the SAME task_id - reading the row fresh inside
    this transaction, never trusting a caller's own guess, the same
    cross-connection-safe pattern already used by #30's own dispatch
    intent reservation. BUG_FOUND is emitted with `emit_in_transaction`
    in the SAME transaction that creates the Task/episode row (round-2
    finding #1 - previously emitted after the transaction committed, so
    a crash/interruption between the two left the event permanently
    missing while the Task/Issue had already survived).

    Finding #2 (payload must never change within an open episode):
    GitHubClient's own idempotency hashes the FULL create_issue payload
    (title+body+labels+correlation_id) per correlation_id and returns
    'correlation_conflict' on any mismatch - so recomputing the Issue
    body from each call's own commit/logs broke replay for a still-open
    episode. The title/body used for the WINNING reservation (new
    episode or adopted legacy Task) are persisted on the episode row and
    replayed verbatim on every later call for that same open episode;
    they never track a later call's own commit/logs.

    Finding #4 (adopting a pre-existing Task from before this table
    existed): #142's original scheme used the bare
    sha256([project, kind, detail]) hash itself as `correlation_id`, with
    no separate identity table - `detail_hash` here uses the exact same
    formula, so it doubles as that legacy correlation_id. Before minting
    a brand new episode, an open (non-DONE/FAILED) legacy Task with that
    exact correlation_id and origin='post_deploy_check' is adopted into
    the episodes table instead - same task_id, same correlation_id - so
    GitHubClient's own dedup (keyed on repo+correlation_id) finds the
    Issue it already created instead of posting a second one.

    A resolved episode's identity never lives forever: `episode_id` (a
    fresh uuid4) is only minted when no row exists yet OR the existing
    one is `resolved` - the caller's correlation_id for a NEW episode is
    built FROM this episode_id, so a genuinely new episode always gets a
    genuinely new GitHub correlation, while a still-open one (including
    an adopted legacy one, which keeps ITS OWN original correlation_id)
    keeps reusing the same one.
    """
    def apply(connection):
        row = connection.execute(
            "SELECT episode_id, task_id, resolved, issue_title, issue_body "
            "FROM deploy_failure_episodes WHERE project_id = ? AND kind = ? AND detail_hash = ?",
            (project_id, kind, detail_hash),
        ).fetchone()
        if row is not None and not row[2]:
            existing_task_row = connection.execute(
                "SELECT data FROM tasks WHERE id = ?", (row[1],),
            ).fetchone()
            if existing_task_row is not None:
                return (row[0], Task.from_dict(json.loads(existing_task_row[0])), False, row[3], row[4])

        legacy = connection.execute(
            "SELECT id, data FROM tasks WHERE state NOT IN (?, ?) "
            "AND json_extract(data, '$.correlation_id') = ? "
            "AND json_extract(data, '$.origin') = 'post_deploy_check'",
            (*_CLOSED_TASK_STATES, detail_hash),
        ).fetchone()
        if legacy is not None:
            legacy_task = Task.from_dict(json.loads(legacy[1]))
            connection.execute(
                "INSERT INTO deploy_failure_episodes "
                "(project_id, kind, detail_hash, episode_id, task_id, resolved, issue_title, issue_body) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(project_id, kind, detail_hash) DO UPDATE SET "
                "episode_id = excluded.episode_id, task_id = excluded.task_id, resolved = 0, "
                "issue_title = excluded.issue_title, issue_body = excluded.issue_body",
                (project_id, kind, detail_hash, detail_hash, legacy_task.id, legacy_task.title, issue_body),
            )
            emit_in_transaction(
                connection, EventType.BUG_FOUND,
                {"task_id": legacy_task.id, "project": project_id, "kind": kind, "migrated": True},
                correlation_id=legacy_task.correlation_id, project_id=project_id,
            )
            return detail_hash, legacy_task, True, legacy_task.title, issue_body

        episode_id = str(uuid.uuid4())
        # A short episode suffix keeps a reopened episode's title distinct
        # from the closed one it replaces - GitHubClient's own dedup falls
        # back to a case-insensitive TITLE match when a correlation marker
        # isn't found yet (e.g. a legacy issue), and two genuinely separate
        # episodes sharing an identical title would otherwise collide there.
        titled = f"{task.title} [{episode_id[:8]}]"
        new_task = Task(
            title=titled, objective=task.objective,
            project_id=task.project_id, origin=task.origin, state=task.state,
            correlation_id=f"deploy-failure:{project_id}:{kind}:{detail_hash}:{episode_id}",
        )
        connection.execute(
            "INSERT INTO tasks (id, state, data) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, data=excluded.data",
            (new_task.id, new_task.state.value, json.dumps(new_task.to_dict())),
        )
        connection.execute(
            "INSERT INTO deploy_failure_episodes "
            "(project_id, kind, detail_hash, episode_id, task_id, resolved, issue_title, issue_body) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(project_id, kind, detail_hash) DO UPDATE SET "
            "episode_id = excluded.episode_id, task_id = excluded.task_id, resolved = 0, "
            "issue_title = excluded.issue_title, issue_body = excluded.issue_body",
            (project_id, kind, detail_hash, episode_id, new_task.id, titled, issue_body),
        )
        emit_in_transaction(
            connection, EventType.BUG_FOUND,
            {"task_id": new_task.id, "project": project_id, "kind": kind},
            correlation_id=new_task.correlation_id, project_id=project_id,
        )
        return episode_id, new_task, True, titled, issue_body

    store.ensure_schema(_EPISODE_SCHEMA)
    return store.run_in_transaction(apply)


def _format_bug_body(
    project_context, result: SmokeCheckResult, *, commit: str | None, logs: str | None,
) -> str:
    lines = [
        f"Projeto: {project_context.canonical_id}",
        f"Commit: {commit or 'desconhecido - nao informado pelo chamador'}",
        f"Branch: {project_context.default_branch or 'desconhecida'}",
        "Origin: post_deploy_check",
        "",
        "## ESPERADO",
        result.expected or "(nao especificado)",
        "",
        "## OBSERVADO",
        result.observed or result.detail,
        "",
        "## REPRODUCAO",
        f"Smoke check automatico pos-deploy ({result.kind}, verified={result.verified}).",
        "",
        "## EVIDENCIA",
        result.detail,
        "",
        "## LOGS",
        logs or "(nenhum log fornecido)",
        "",
        "## AREA PROVAVEL",
        result.kind,
        "",
        "## SEVERIDADE",
        "emergencia (suspeita de producao inacessivel)" if result.emergency else "normal",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class FailureReport:
    task: Task
    episode_id: str
    is_new_episode: bool
    issue_available: bool | None  # None when no client/repository at all
    issue_reason: str | None
    notified: bool


def report_smoke_check_failure(
    project_context, result: SmokeCheckResult, store: Store, *,
    client: GitHubClient | None = None, commit: str | None = None, logs: str | None = None,
) -> FailureReport:
    """Records a failed smoke check against a stable failure-EPISODE
    identity (see `_reserve_episode`) - repeated identical failures
    within the SAME still-open episode reuse the same Task/GitHub
    Issue/Lucas notification; a NEW episode (first occurrence, or a
    recurrence after the previous one resolved) mints a fresh identity
    for all three. Only pages Lucas (#33's flow) for a genuine emergency,
    and only ONCE per episode - repeated calls for an already-notified
    open episode reuse decisions.py's own idempotent delivery guard
    (same task/correlation_id -> same decision, confirmed at most once).

    `client` defaults to a plain `GitHubClient(store)` when omitted
    (Review Task #143 finding #5: the previous default silently created
    no Issue at all even with a valid `repository` configured) - pass an
    explicit one to reuse a caller's own config/session. The real Issue
    creation outcome is always surfaced via `FailureReport.issue_available`/
    `issue_reason`, never silently discarded.
    """
    kind = result.kind
    detail_hash = _detail_hash(project_context, result)
    title = f"[smoke check] {project_context.canonical_id}: {result.detail}"
    body = _format_bug_body(project_context, result, commit=commit, logs=logs)
    placeholder = Task(
        title=title, objective=result.detail, project_id=project_context.canonical_id,
        origin="post_deploy_check",
        state=TaskState.NEEDS_LUCAS if result.emergency else TaskState.BUG_FOUND,
    )
    episode_id, task, is_new_episode, issue_title, issue_body = _reserve_episode(
        store, project_context.canonical_id, kind, detail_hash, placeholder, title, body,
    )

    issue_available: bool | None = None
    issue_reason: str | None = None
    if project_context.repository:
        gh_client = client or GitHubClient(store)
        issue_result = gh_client.create_issue(
            project_context.repository, issue_title, issue_body,
            ["origin:post_deploy_check"], task.correlation_id,
        )
        issue_available = bool(issue_result.get("available"))
        issue_reason = issue_result.get("reason")

    notified = False
    if result.emergency:
        ok, _error = notify_needs_lucas(
            task,
            problem=f"{project_context.canonical_id} pode estar fora do ar apos o deploy.",
            why=(
                "Smoke check pos-deploy nao conseguiu alcancar a producao em uma sondagem - "
                "suspeita de queda, ainda nao confirmada por multiplas verificacoes."
            ),
            options=["Investigar agora", "Aguardar e reverificar", "Reverter o ultimo deploy"],
            recommendation="Investigar agora - se confirmado, bloqueia todos os usuarios.",
            impact="Producao pode estar indisponivel para os usuarios enquanto isso.",
            store=store,
        )
        notified = ok

    return FailureReport(
        task=task, episode_id=episode_id, is_new_episode=is_new_episode,
        issue_available=issue_available, issue_reason=issue_reason, notified=notified,
    )


def resolve_episode(store: Store, project_context, kind: str, detail: str) -> None:
    """Marks a failure episode resolved, so its NEXT recurrence (if any)
    is treated as a genuinely new episode (see `_reserve_episode`) rather
    than reusing a Task/Issue that has already been closed out. Never
    called automatically by this module - #38 has no automatic
    resolution detection; whoever confirms the fix (a human, or a future
    re-check) calls this explicitly."""
    detail_hash = hashlib.sha256(
        json.dumps([project_context.canonical_id, kind, detail], sort_keys=True).encode("utf-8"),
    ).hexdigest()

    def apply(connection):
        connection.execute(
            "UPDATE deploy_failure_episodes SET resolved = 1 "
            "WHERE project_id = ? AND kind = ? AND detail_hash = ?",
            (project_context.canonical_id, kind, detail_hash),
        )

    store.ensure_schema(_EPISODE_SCHEMA)
    store.run_in_transaction(apply)
