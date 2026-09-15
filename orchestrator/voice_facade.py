"""Facade between the voice dispatcher (main.py) and orchestrator/ (#11, #35).

The SINGLE contact point between the ~2000-line main.py monolith and this
package (PROMPT MESTRE V2 section 6/56) - main.py never imports any other
orchestrator module directly for voice. Every answer here is built from
REAL data read through #13 (persistence), #14 (events, via #34's history
module) and #18 (paperclip_client) - never invented. When there is
genuinely nothing to report, that is said plainly ("nada bloqueado",
"nada registrado") rather than manufacturing an answer.

main.py's own call sites are unchanged from #11 - only the CONTENT of
these three functions changed, per this issue's own scope note.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import paperclip_client
from orchestrator import history
from orchestrator.config import load_config
from orchestrator.models import TaskState
from orchestrator.persistence import Store
from orchestrator.project_resolver import ProjectContext, ProjectResolver

_PAPERCLIP_UNAVAILABLE = "Não consegui falar com o Paperclip agora, senhor"

_store_lock = threading.Lock()
_store: Store | None = None


def _normalize(name: str) -> str:
    """Strips separators/case so 'Masya Studio', 'Masya_Studio' and
    'masya-studio' compare equal - but NOT so far that distinct projects
    collapse together (see _project_answer's own comment)."""
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


def _get_store() -> Store:
    """Lazily opens the ONE Store instance this facade uses for the life
    of the process (matching persistence.py's own "one Store per
    process" design) - tests replace this via monkeypatch rather than
    threading a store parameter through every call, since main.py's own
    call sites (fixed by #11) pass none."""
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def _start_of_today() -> datetime:
    """Midnight in the orchestrator's configured local timezone (#25's
    own Scheduler uses the exact same ZoneInfo(config.timezone) pattern),
    converted to UTC for history.diff_since - so "hoje" means the same
    calendar day the daily cycle itself uses, not a rolling 24h window."""
    config = load_config()
    tz = ZoneInfo(config.timezone)
    now_local = datetime.now(tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc)


def _blocked_answer(store: Store) -> str:
    blocked = store.list_tasks(state=TaskState.BLOCKED)
    if not blocked:
        return "Nada bloqueado no momento, senhor."
    titles = [task.title for task in blocked[:5]]
    extra = f", e mais {len(blocked) - 5}" if len(blocked) > 5 else ""
    return f"{len(blocked)} tarefa(s) bloqueada(s), senhor: {'; '.join(titles)}{extra}."


def _needs_me_answer(store: Store) -> str:
    waiting_tasks = store.list_tasks(state=TaskState.NEEDS_LUCAS)
    pending_decisions = store.get_pending_decisions()
    if not waiting_tasks and not pending_decisions:
        return "Nada precisando de você agora, senhor."
    # Independent ifs, not elif: a pending decision and a NEEDS_LUCAS task
    # with no matching decision record are both real, and can coexist -
    # decisions.py/review_pipeline.py set NEEDS_LUCAS directly in places
    # without guaranteeing a save_decision call for that exact task, so
    # only reporting whichever list happens to be checked first would
    # silently under-report the other.
    parts = []
    if pending_decisions:
        preview = "; ".join(d["message"][:120] for d in pending_decisions[:2])
        extra = f", e mais {len(pending_decisions) - 2}" if len(pending_decisions) > 2 else ""
        parts.append(f"{len(pending_decisions)} pergunta(s) pendente(s): {preview}{extra}")
    if waiting_tasks:
        parts.append(f"{len(waiting_tasks)} tarefa(s) esperando sua decisão")
    return ", ".join(parts) + "."


def _who_is_working_answer() -> str:
    snapshot = paperclip_client.get_snapshot()
    if not snapshot.get("available"):
        return f"{_PAPERCLIP_UNAVAILABLE} - {snapshot.get('reason', 'motivo desconhecido')}."
    agents = [
        (company["name"], agent)
        for company in snapshot.get("companies", [])
        for agent in company.get("agents", [])
    ]
    if not agents:
        return "Não há nenhum agente configurado no Paperclip ainda, senhor."
    bits = []
    for company_name, agent in agents:
        bit = f"{agent.get('name', '?')} ({company_name}): {agent.get('status') or 'status desconhecido'}"
        if agent.get("pause_reason"):
            bit += f", pausado por {agent['pause_reason']}"
        if agent.get("error_reason"):
            bit += f", em erro: {agent['error_reason']}"
        bits.append(bit)
    return "; ".join(bits) + "."


def _project_answer(store: Store, project: ProjectContext) -> str:
    local_tasks = [
        task for task in store.list_tasks()
        if (task.project_id or "").strip().lower() == project.canonical_id.strip().lower()
    ]
    lines = []
    if local_tasks:
        counts: dict[str, int] = {}
        for task in local_tasks:
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        lines.append("Na orquestração: " + ", ".join(f"{n} em {state}" for state, n in counts.items()))

    snapshot = paperclip_client.get_snapshot()
    if snapshot.get("available"):
        # Exact match after normalization only (never a free substring
        # check either direction) - project_resolver.py's own docstring
        # explicitly documents near-homonym traps like "Argos" vs
        # "Argos-Hub" or "Cashy" vs "Cashy-Android"; a substring match
        # would silently attribute one project's Paperclip data to a
        # DIFFERENT, similarly-named one.
        target = _normalize(project.canonical_id)
        for company in snapshot.get("companies", []):
            if _normalize(company.get("name") or "") == target:
                by_status = company.get("issues_by_status") or {}
                if by_status:
                    lines.append("No Paperclip: " + ", ".join(f"{n} {status}" for status, n in by_status.items()))
                break

    if not lines:
        return f"Nada registrado sobre {project.canonical_id} no momento, senhor."
    return " ".join(lines)


def handle_status_query(query: str) -> str | None:
    """Routes a status question to the specific real answer it's asking
    for. Returns `None` only when this facade genuinely doesn't recognize
    the shape of the question (letting the caller fall through to its own
    unavailable message) - never a fabricated guess."""
    q = query.lower()
    store = _get_store()

    if any(p in q for p in ("bloqueado", "bloqueios", "travad")):
        return _blocked_answer(store)
    if any(p in q for p in ("precisa de mim", "precisa da minha", "preciso decidir")):
        return _needs_me_answer(store)
    if any(p in q for p in ("quem esta trabalhando", "quem está trabalhando", "quem trabalha", "quem esta ativo")):
        return _who_is_working_answer()

    resolved = ProjectResolver().resolve_from_text(query)
    if isinstance(resolved, ProjectContext):
        return _project_answer(store, resolved)

    return None


def handle_report_query() -> str:
    """Unconditional daily-activity summary since the start of the
    orchestrator's local calendar day (#34's own history module) -
    matches the "o que a equipe fez hoje" example directly."""
    store = _get_store()
    summary = history.diff_since(store, since=_start_of_today())
    return history.summarize_for_voice(summary)


def handle_control_query(query: str) -> str | None:
    """No real pause/resume mechanism exists anywhere in the orchestrator
    yet (no persisted flag, no scheduler wiring) - inventing an
    acknowledgement for an action that doesn't actually happen would
    violate this module's own "never invent state" rule, so this stays
    an honest limitation rather than a fake "done, senhor"."""
    q = query.lower()
    if any(p in q for p in ("pausar", "retomar", "controle")):
        return (
            "Ainda não tenho um jeito real de pausar ou retomar a orquestração, senhor "
            "- por enquanto só consigo reportar o estado dela."
        )
    return None
