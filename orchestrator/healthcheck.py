"""Observed subsystem health (#36); no recovery, service startup or retry loop."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
from dataclasses import dataclass, field
from collections.abc import Callable
import paperclip_client as client
from orchestrator.agent_availability import is_agent_available
from orchestrator.audit import record as audit_record
from orchestrator.events import EventType, emit_in_transaction, query_events
from orchestrator.models import AgentName, TaskState
from orchestrator.task_queue import get_promotable_tasks
from datetime import datetime, timezone

import paperclip_client
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.persistence import Store

_RUNTIME_COMPONENTS = ('jarvis', 'planner', 'scheduler')


@dataclass(frozen=True)
class ComponentHealth:
    status: str
    detail: str
    observed_at: datetime | None = None


@dataclass
class HealthReport:
    checked_at: datetime
    components: dict[str, ComponentHealth] = field(default_factory=dict)
    pending_actions: int | None = 0
    failed_actions: int | None = 0
    rate_limited_agents: list[str] = field(default_factory=list)
    last_cycle_at: datetime | None = None
    storage_ok: bool = True

    @property
    def ok(self) -> bool:
        return (self.storage_ok and len(self.components) == 6
                and all(item.status == 'ok' for item in self.components.values())
                and self.pending_actions == 0 and self.failed_actions == 0
                and not self.rate_limited_agents)


def _utc(at: datetime | None = None) -> datetime:
    at = at if at is not None else datetime.now(timezone.utc)
    if at.utcoffset() is None:
        raise ValueError('Health timestamps must be timezone-aware')
    return at.astimezone(timezone.utc)


def _timestamp(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return _utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def record_heartbeat(store: Store, component: str, *, at: datetime | None = None) -> None:
    """Runtime calls periodically while this component is alive, including idle."""
    if component not in _RUNTIME_COMPONENTS:
        raise ValueError('Unknown runtime component')
    store.set_sync_value(f'health:{component}:heartbeat', _utc(at).isoformat())


def _telegram_key(config: OrchestratorConfig, channel: str) -> str:
    if channel not in ('control', 'report'):
        raise ValueError('Unknown Telegram channel')
    chat = config.telegram_control_chat_id if channel == 'control' else config.telegram_report_chat_id
    # Credentials/chat changes must not inherit the old destination's success.
    fingerprint = hashlib.sha256(json.dumps([config.telegram_bot_token, chat]).encode()).hexdigest()
    return f'health:telegram:{channel}:{fingerprint}'


def record_telegram_delivery(store: Store, config: OrchestratorConfig, channel: str,
                             ok: bool, *, at: datetime | None = None) -> None:
    """Record only status/time, never token, chat, message text or raw error."""
    if not isinstance(ok, bool):
        raise ValueError('Delivery status must be boolean')
    now = _utc(at)
    key = _telegram_key(config, channel)
    def save(connection):
        row = connection.execute('SELECT value FROM sync_state WHERE key=?', (key,)).fetchone()
        previous = {}
        if row:
            try:
                previous = json.loads(row[0])
            except (ValueError, TypeError):
                pass
        if not isinstance(previous, dict):
            previous = {}
        last_attempt = _timestamp(previous.get('attempt_at'))
        if last_attempt is not None and last_attempt > now:
            return
        value = {'ok': ok, 'attempt_at': now.isoformat(),
                 'success_at': now.isoformat() if ok else previous.get('success_at')}
        connection.execute('INSERT INTO sync_state (key,value) VALUES (?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))
    store.run_in_transaction(save)


def _probe_github() -> bool:
    result = subprocess.run(['gh', 'api', '--hostname', 'github.com', '/rate_limit'],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout)
    remaining = data['resources']['core']['remaining']
    return isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0


def _observed_probe(probe, now: datetime) -> ComponentHealth:
    try:
        ok = probe() is True
    except Exception:
        ok = False
    return ComponentHealth('ok' if ok else 'offline',
                           'Sonda de leitura confirmada.' if ok else 'Sonda indisponivel ou resposta invalida.', now)


def _runtime_health(value, now, max_age) -> ComponentHealth:
    last = _timestamp(value)
    if last is None or last > now:
        return ComponentHealth('unknown', 'Sem sinal recente e valido do runtime.')
    if (now - last).total_seconds() > max_age:
        return ComponentHealth('stale', 'Sinal de atividade expirado.', last)
    return ComponentHealth('ok', 'Atividade recente confirmada pelo runtime.', last)


def _telegram_health(values, config, now, max_age) -> ComponentHealth:
    if not all((config.telegram_bot_token, config.telegram_control_chat_id, config.telegram_report_chat_id)):
        return ComponentHealth('unconfigured', 'Token ou um dos dois canais nao configurado.')
    observations = []
    for channel in ('control', 'report'):
        try:
            data = json.loads(values.get(_telegram_key(config, channel), 'null'))
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            observations.append(ComponentHealth('unknown', f'Sem envio observado no canal {channel}.'))
            continue
        attempt = _timestamp(data.get('attempt_at'))
        success = _timestamp(data.get('success_at'))
        if attempt is None or attempt > now or (now - attempt).total_seconds() > max_age:
            observations.append(ComponentHealth('unknown', f'Evidencia de envio ausente/antiga no canal {channel}.', attempt))
        elif data.get('ok') is False:
            observations.append(ComponentHealth('offline', f'Ultima tentativa falhou no canal {channel}.', attempt))
        elif data.get('ok') is True and success == attempt:
            observations.append(ComponentHealth('ok', f'Envio confirmado no canal {channel}.', success))
        else:
            observations.append(ComponentHealth('unknown', f'Evidencia de envio invalida no canal {channel}.'))
    for status in ('offline', 'unknown'):
        for observation in observations:
            if observation.status == status:
                return observation
    return ComponentHealth('ok', 'Envios confirmados nos dois canais.', min(o.observed_at for o in observations))


def get_health_status(*, store: Store | None = None, config: OrchestratorConfig | None = None,
                      clock=None, paperclip_probe=None, github_probe=None,
                      heartbeat_max_age_seconds: float = 120,
                      telegram_max_age_seconds: float = 172800) -> HealthReport:
    """Read bounded probes and persisted observations; missing evidence is unknown.

    pending_actions counts retryable/in-flight GitHub operations plus unresolved
    Paperclip claims. Terminal GitHub failures are reported separately.
    Runtime heartbeat production belongs to #30/#27, not this diagnostic call.
    """
    for age in (heartbeat_max_age_seconds, telegram_max_age_seconds):
        if not math.isfinite(age) or age <= 0:
            raise ValueError('Health freshness windows must be finite and positive')
    cfg = config or load_config()
    now = _utc(clock() if clock else None)
    report = HealthReport(now)
    report.components['paperclip'] = _observed_probe(paperclip_probe or (
        lambda: paperclip_client.is_available(base_url=cfg.paperclip_base_url,
                                               timeout=cfg.paperclip_timeout_seconds)), now)
    report.components['github'] = _observed_probe(github_probe or _probe_github, now)
    owned = store is None
    active_store = store
    try:
        active_store = active_store if active_store is not None else Store()
        # One short DB snapshot; probes above never run while holding its lock.
        def snapshot(connection):
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            pending, failed = 0, 0
            if 'pending_github_ops' in tables:
                for status, count in connection.execute('SELECT status, COUNT(*) FROM pending_github_ops GROUP BY status'):
                    if status == 'failed':
                        failed += count
                    elif status != 'done':
                        pending += count
            if 'paperclip_creations' in tables:
                pending += connection.execute('SELECT COUNT(*) FROM paperclip_creations WHERE result IS NULL').fetchone()[0]
            return (dict(connection.execute('SELECT key,value FROM sync_state')),
                    list(connection.execute('SELECT agent,reset_at FROM rate_limits')), pending, failed)
        values, limits, report.pending_actions, report.failed_actions = active_store.run_in_transaction(snapshot)
        for component in _RUNTIME_COMPONENTS:
            report.components[component] = _runtime_health(values.get(f'health:{component}:heartbeat'), now, heartbeat_max_age_seconds)
        report.components['telegram'] = _telegram_health(values, cfg, now, telegram_max_age_seconds)
        cycles = [_timestamp(value) for key, value in values.items()
                  if key.startswith('scheduler:') and key.endswith(':cycle_start')]
        report.last_cycle_at = max((at for at in cycles if at is not None and at <= now), default=None)
        report.rate_limited_agents = sorted(agent for agent, reset in limits
                                             if _timestamp(reset) is None or _timestamp(reset) > now)
    except sqlite3.Error:
        report.storage_ok = False
        report.pending_actions = report.failed_actions = None
        for component in (*_RUNTIME_COMPONENTS, 'telegram'):
            report.components[component] = ComponentHealth('unknown', 'Estado persistido indisponivel.')
    finally:
        if owned and active_store is not None:
            active_store.close()
    return report


_LABELS = {'jarvis': 'Jarvis', 'paperclip': 'Paperclip', 'github': 'GitHub',
           'telegram': 'Telegram', 'planner': 'Planner', 'scheduler': 'Scheduler'}
_STATUSES = {'ok': 'OK', 'offline': 'indisponivel', 'unconfigured': 'nao configurado',
             'unknown': 'nao confirmado', 'stale': 'atividade desatualizada'}


def format_health_for_voice(report: HealthReport) -> str:
    if report.ok:
        return 'Todos os subsistemas com evidencias recentes de funcionamento; sem acoes pendentes ou agentes em cooldown.'
    parts = [f'{_LABELS[name]}: {_STATUSES[item.status]}' for name, item in report.components.items()
             if item.status != 'ok']
    if not report.storage_ok:
        parts.append('estado local indisponivel; contagens nao confirmadas')
    if report.pending_actions:
        parts.append(f'{report.pending_actions} acao(oes) pendente(s)')
    if report.failed_actions:
        parts.append(f'{report.failed_actions} acao(oes) com falha definitiva')
    if report.rate_limited_agents:
        parts.append(f'{len(report.rate_limited_agents)} agente(s) em cooldown')
    return 'Atencao: ' + '; '.join(parts) + '.'


def format_health_for_telegram(report: HealthReport) -> str:
    lines = [f'Saude do Jarvis - {report.checked_at.isoformat()}']
    for name in _LABELS:
        item = report.components[name]
        lines.append(f'{_LABELS[name]}: {_STATUSES[item.status]}. {item.detail}')
    lines.append(f'Acoes pendentes: {report.pending_actions if report.pending_actions is not None else "nao confirmado"}.')
    lines.append(f'Falhas definitivas: {report.failed_actions if report.failed_actions is not None else "nao confirmado"}.')
    lines.append(f'Agentes em cooldown: {len(report.rate_limited_agents) if report.storage_ok else "nao confirmado"}.')
    lines.append('Ultimo ciclo observado: ' + (report.last_cycle_at.isoformat() if report.last_cycle_at else 'nao confirmado') + '.')
    return '\n'.join(lines)


# Idle diagnosis from issue #27; preserved during integration with #36.
# Fixed per Codex's review (Review Task #113, 4 findings) - see check_idle.
_ACTIVITY_EVENTS = [EventType.TASK_STARTED, EventType.TASK_COMPLETED]


@dataclass(frozen=True)
class IdleDiagnosis:
    cause: str
    detail: str
    ready_task_ids: tuple[str, ...]
    escalated: bool


def _now(clock: Callable[[], datetime] | None) -> datetime:
    instant = (clock or (lambda: datetime.now(timezone.utc)))()
    if instant.utcoffset() is None:
        raise ValueError("healthcheck clock must return a timezone-aware datetime")
    return instant.astimezone(timezone.utc)


def _last_activity_at(store: Store) -> datetime | None:
    events = query_events(store, event_types=_ACTIVITY_EVENTS)
    return events[-1]["created_at"] if events else None


def _paused_agents(snapshot: dict) -> list[str]:
    """Agents with a recorded pause_reason in Paperclip's own snapshot
    (#11) - a stronger signal than guessing at the exact `status` string
    vocabulary. Returns [] on any unavailable/malformed snapshot rather
    than raising (get_snapshot() itself never raises, but its shape is
    still untrusted input)."""
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return []
    names = []
    for company in snapshot.get("companies") or []:
        for agent in (company or {}).get("agents") or []:
            if isinstance(agent, dict) and agent.get("pause_reason"):
                names.append(agent.get("name") or "desconhecido")
    return names


def _record(store: Store, diagnosis: IdleDiagnosis) -> None:
    audit_record(
        store, action="idle_check", origin="healthcheck",
        result="escalated" if diagnosis.escalated else "diagnosed",
        extra={
            "cause": diagnosis.cause,
            "ready_task_ids": list(diagnosis.ready_task_ids),
        },
    )


def check_idle(
    store: Store,
    *,
    config: OrchestratorConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    paperclip_available: Callable[[], bool] | None = None,
    paperclip_snapshot: Callable[[], dict] | None = None,
) -> IdleDiagnosis | None:
    """Returns a diagnosis only when apparent idleness is real and either
    explained (Paperclip down, a paused agent, a dependency inconsistency)
    or genuinely unexplained - `None` whenever there simply is no stall to
    explain (no READY work, no free agent, work already in flight, or the
    quiet period hasn't crossed the threshold yet).

    Only tasks NOT blocked by an unmet dependency count toward the
    "genuinely idle" set: a single blocked READY task must never hide
    OTHER READY work that has nothing stopping it (Review Task #113,
    finding #1) - if every READY task turns out to be blocked, that is
    reported instead, unescalated.

    The grace period is evaluated PER TASK, not once for the whole free/
    ready set: each task's own reference instant is whichever is more
    recent of its own `updated_at` or the last TASK_STARTED/TASK_COMPLETED
    event, and only tasks whose reference is already older than
    `idle_check_minutes` count as genuinely stale. Gating on a single
    shared reference (the MOST RECENT among them) was tried and rejected
    (Review Task #113, round 3): a brand-new READY task arriving next to
    an old, genuinely stalled one kept resetting the shared clock, so the
    old one could be masked forever by a steady trickle of new arrivals.
    Only the stale subset is ever reported or escalated - a fresh task
    that hasn't earned its own grace period yet is simply not part of the
    diagnosis. A task's own `updated_at` is always a real, present anchor
    even before any activity event exists (round 1, finding #2 - a brand-
    new database must not be read as "idle forever"), and anchoring
    included EVERY task's updated_at (not just the free/stalled ones) was
    also tried and rejected (round 2, finding #2): an unrelated BLOCKED
    task getting touched moments ago must not mask a stale free task.

    A given idle episode escalates via DECISION_REQUIRED at most once
    (finding #3): a periodic poller must not re-ask the same question
    every tick while nothing about the stall has changed. An episode is
    identified by the stale subset's task ids TOGETHER WITH each one's
    own reference instant - either changing (a task recovering then
    stalling again, a TASK_COMPLETED event moving that task's floor)
    means the earlier stall ended and a later stall of the same ids is a
    genuinely new episode that must escalate again (round 2, finding #1).
    The returned `escalated` (and the audit log) reflect whether THIS
    call actually emitted a fresh escalation, not just whether the cause
    is "unexplained" - a repeated poll of an already-escalated episode is
    correctly a no-op, not another "escalated" outcome.

    A reachable Paperclip is also checked for a paused agent (Review Task
    #113, finding #4: "CEO parece ativo?") via its existing snapshot
    (#11) - if found, that IS the probable cause and is reported as such
    without escalating. Attempting to un-pause it automatically is
    deliberately NOT done here: the issue's own OUT OF SCOPE covers
    correcao automatica, and unconditionally resuming a paused agent
    could just as easily undo a deliberate pause (budget exhaustion, a
    manual decision) as fix a stuck one - indistinguishable from the data
    this module has access to.
    """
    cfg = config or load_config()
    now = _now(clock)

    all_tasks = store.list_tasks()
    ready = [task for task in all_tasks if task.state == TaskState.READY]
    if not ready:
        return None

    if any(task.state == TaskState.IN_PROGRESS for task in all_tasks):
        return None

    agent_free = (
        is_agent_available(store, AgentName.CLAUDE, clock=clock)
        or is_agent_available(store, AgentName.CODEX, clock=clock)
    )
    if not agent_free:
        return None

    promotable_ids = {task.id for task in get_promotable_tasks(all_tasks)}
    free_ready = [task for task in ready if task.id in promotable_ids]
    blocked_ready_ids = tuple(task.id for task in ready if task.id not in promotable_ids)

    if not free_ready:
        diagnosis = IdleDiagnosis(
            cause="dependency_blocked",
            detail="Tarefa(s) READY tem dependencia ainda nao concluida - inconsistencia de estado, nao ociosidade real.",
            ready_task_ids=blocked_ready_ids,
            escalated=False,
        )
        _record(store, diagnosis)
        return diagnosis

    last_activity = _last_activity_at(store)

    def task_reference(task) -> datetime:
        return max(task.updated_at, last_activity) if last_activity is not None else task.updated_at

    stale = [
        task for task in free_ready
        if (now - task_reference(task)).total_seconds() / 60 >= cfg.idle_check_minutes
    ]
    if not stale:
        return None

    free_ready_ids = tuple(task.id for task in stale)

    check_paperclip = paperclip_available or client.is_available
    if not check_paperclip():
        diagnosis = IdleDiagnosis(
            cause="paperclip_unavailable",
            detail="Paperclip nao respondeu ao healthcheck - tarefas READY nao podem ser despachadas.",
            ready_task_ids=free_ready_ids,
            escalated=False,
        )
        _record(store, diagnosis)
        return diagnosis

    get_snapshot = paperclip_snapshot or client.get_snapshot
    paused = _paused_agents(get_snapshot())
    if paused:
        diagnosis = IdleDiagnosis(
            cause="agent_paused",
            detail=f"Agente(s) pausado(s) no Paperclip: {', '.join(paused)} - provavel causa da ociosidade.",
            ready_task_ids=free_ready_ids,
            escalated=False,
        )
        _record(store, diagnosis)
        return diagnosis

    episode_signature = ",".join(
        f"{task.id}:{task_reference(task).isoformat()}"
        for task in sorted(stale, key=lambda task: task.id)
    )
    episode_id = hashlib.sha256(episode_signature.encode("utf-8")).hexdigest()[:16]

    def escalate(connection):
        emit_in_transaction(
            connection, EventType.DECISION_REQUIRED,
            {"kind": "idle_stall", "cause": "unexplained", "ready_task_ids": list(free_ready_ids)},
            correlation_id=episode_id, created_at=now,
        )

    newly_escalated = store.run_sync_once(f"idle_escalation:{episode_id}", now.isoformat(), escalate)
    diagnosis = IdleDiagnosis(
        cause="unexplained",
        detail="Tarefas READY, agente disponivel e Paperclip ok, mas nada em andamento ha mais tempo que o esperado.",
        ready_task_ids=free_ready_ids,
        escalated=newly_escalated,
    )
    _record(store, diagnosis)
    return diagnosis
