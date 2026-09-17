"""Executes a plan the user just confirmed (issue #152): real GitHub
Issues (#23) and real Paperclip agent assignment (#26/#30) - the two
things `decisions.py`'s own `_route_control` deliberately never does on
its own, since they're exactly the kind of hard-to-reverse, externally-
visible action that needs the human's own explicit "sim" first.

Kept as a SEPARATE module from decisions.py on purpose: orchestrator.py
(which owns `run_daily_cycle`) already imports FROM decisions.py
(`get_priority_queue`), so decisions.py importing back from orchestrator.py
would be a circular import - `handle_control_message`'s `confirm_fn`
parameter is the seam that lets this module depend on decisions.py's
`ControlResult`-free callable contract without decisions.py depending on
this one.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import paperclip_client
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.github_client import GitHubClient
from orchestrator.models import TaskState
from orchestrator.orchestrator import run_daily_cycle
from orchestrator.paperclip_ops import PaperclipSession
from orchestrator.persistence import Store
from orchestrator.project_resolver import ProjectContext, ProjectResolver, ResolveError

_NO_PROJECT = "Nao consegui identificar de novo o projeto '{project_id}' - o plano ficou apenas registrado localmente, sem Issue nem agente."
_NO_TASKS = "O plano nao tinha nenhuma tarefa valida para executar."
_NO_COMPANY = (
    "Identifiquei o projeto '{project_id}', mas nao encontrei uma empresa correspondente no Paperclip "
    "(procurei por '{project_id}') - as tarefas ficaram publicadas no GitHub, mas nenhum agente foi acionado."
)


def _normalize(name: str) -> str:
    """Exact match after stripping separators/case only - never a free
    substring check (same near-homonym reasoning as #149's voice_facade:
    'argos' must never match 'Argos-Hub')."""
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


def resolve_company_id(project: ProjectContext, config: OrchestratorConfig) -> str | None:
    companies, err = paperclip_client.list_companies()
    if err:
        return None
    target = _normalize(project.canonical_id)
    for company in companies:
        if _normalize(company.get("name") or "") == target:
            return company.get("id")
    return None


def admission_window_clock(config: OrchestratorConfig):
    """A real-time, user-confirmed "sim" is its own authorization to
    start now - it does not need to wait for the automatic daily cycle's
    own pacing window (default 08:00-14:00), which exists to spread
    AUTOMATIC admission across the day, not to block an explicit human
    command. Forcing `run_daily_cycle`'s clock to a fixed instant safely
    INSIDE the configured window reuses its existing, already-reviewed
    admission/dispatch logic (`Scheduler._open`: `start <= now < cutoff`)
    unmodified, rather than reimplementing it.

    Picks the midpoint between `cycle_start_time` and `cutoff_time`
    (clamped 1 minute short of cutoff) using real `timedelta` arithmetic
    - not a naive "+30 minutes" that silently overshot the window (and
    even wrapped incorrectly past the hour) for any config where the two
    times are close together. A misconfigured window (cutoff at or
    before start) has no valid instant to force; this returns None to
    let the caller surface that honestly instead of silently dispatching
    nothing while claiming success."""
    tz = ZoneInfo(config.timezone)
    today = datetime.now(tz).date()

    def _parse(value: str) -> datetime:
        hour, minute = (int(part) for part in value.split(":"))
        return datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)

    start = _parse(config.cycle_start_time)
    cutoff = _parse(config.cutoff_time)
    if cutoff <= start:
        return None
    latest_safe = cutoff - timedelta(minutes=1)
    forced = min(start + (cutoff - start) / 2, latest_safe)
    return lambda: forced


def execute_confirmed_plan(
    pending_plan: dict, *, store: Store, config: OrchestratorConfig | None = None,
    client: GitHubClient | None = None, paperclip_session: PaperclipSession | None = None,
    resolver: ProjectResolver | None = None,
) -> str:
    """Materializes and dispatches the tasks referenced by `pending_plan`
    (as saved by `decisions.save_pending_plan`). Never raises - the
    caller (`decisions._route_control`) already wraps this, but every
    failure here degrades to an honest status message naming exactly
    what did and did not happen, never a fabricated success.

    `client`/`paperclip_session`/`resolver` default to real instances
    (same `X or RealThing(...)` pattern used throughout this codebase) -
    injected in tests so they never make a real `gh` CLI call or a real
    Paperclip session against production credentials."""
    cfg = config or load_config()
    project_id = pending_plan.get("project_id")
    task_ids = pending_plan.get("task_ids") or []

    tasks = [task for task_id in task_ids if (task := store.get_task(task_id)) is not None]
    if not tasks:
        return _NO_TASKS

    if not project_id:
        return _NO_PROJECT.format(project_id="(desconhecido)")
    resolved = (resolver or ProjectResolver()).resolve(project_id)
    if isinstance(resolved, ResolveError):
        return _NO_PROJECT.format(project_id=project_id)
    project = resolved

    clock = admission_window_clock(cfg)
    if clock is None:
        return (
            f"CYCLE_START_TIME/CUTOFF_TIME estao configurados de um jeito que nao deixa nenhum horario "
            f"valido para despachar agora ({cfg.cycle_start_time} / {cfg.cutoff_time}) - as tarefas "
            "ficaram registradas localmente, mas nada foi publicado nem atribuido. Corrija a "
            "configuracao e responda \"sim\" de novo."
        )

    company_id = resolve_company_id(project, cfg)
    if paperclip_session is None and company_id:
        paperclip_session = PaperclipSession(config=cfg)

    result = run_daily_cycle(
        store, project,
        client=client or GitHubClient(store, config=cfg),
        paperclip_session=paperclip_session, company_id=company_id,
        clock=clock, config=cfg,
    )

    # Never opens with a claim of success (independent-review finding):
    # the sentence only appears once something real actually happened.
    parts = []
    if result.created_issue_numbers or result.assigned_task_ids:
        parts.append(f"Plano para '{pending_plan.get('objective', project_id)}' executado, senhor.")
    if result.created_issue_numbers:
        numbers = ", ".join(f"#{n}" for n in result.created_issue_numbers)
        parts.append(f"Issues criadas no GitHub: {numbers}.")
    if result.assigned_task_ids:
        parts.append(f"{len(result.assigned_task_ids)} tarefa(s) atribuida(s) a agentes reais.")
    if result.dispatch_incomplete_task_ids:
        parts.append(
            f"{len(result.dispatch_incomplete_task_ids)} tarefa(s) nao confirmaram atribuicao - "
            "verifique manualmente antes de assumir que estao em andamento."
        )
    if company_id is None:
        parts.append(_NO_COMPANY.format(project_id=project.canonical_id))
    if not result.created_issue_numbers and not result.assigned_task_ids:
        remaining = [t for t in tasks if t.state not in (TaskState.DONE, TaskState.FAILED)]
        if remaining:
            parts.insert(0, f"Nada foi despachado ainda para '{pending_plan.get('objective', project_id)}', senhor.")
            parts.append(
                f"({len(remaining)} tarefa(s) continuam pendentes - pode ser falta de agente livre "
                "ou de empresa correspondente no Paperclip.)"
            )
    return " ".join(parts)
