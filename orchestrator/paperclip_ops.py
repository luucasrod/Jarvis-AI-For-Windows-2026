"""Idempotent Paperclip creation and status reads (#18).

All HTTP goes through the existing paperclip_client. SQLite owns the operation
claim; the remote description carries a deterministic marker for reconciliation.
After an uncertain POST we only look up that marker, never blindly POST again.
This favors avoiding duplicate work over guessing that a timed-out write failed.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable

import paperclip_client as client
from orchestrator.audit import record as audit_record
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.paperclip_cache import PaperclipCache
from orchestrator.persistence import Store

_RETRY_DELAYS = (1.0, 2.0, 4.0)


class PaperclipSession:
    """Reusable, thread-safe resilience boundary around the existing #18 API.

    Keep one session per Paperclip server in the runtime. All methods share a
    nonblocking exponential cooldown; they do not sleep, schedule retries, or
    restart the service. Legacy free functions remain available without this
    policy for callers that already manage retry timing themselves.
    """

    def __init__(self, *, config: OrchestratorConfig | None = None,
                 clock=time.monotonic, max_backoff_seconds: float | None = None,
                 sleep_fn: Callable[[float], None] | None = None):
        self.config = config or load_config()
        base = self.config.retry_interval_seconds
        # #44: default comes from config (PAPERCLIP_MAX_BACKOFF_SECONDS)
        # rather than a hardcoded constant - explicit callers still win.
        if max_backoff_seconds is None:
            max_backoff_seconds = self.config.paperclip_max_backoff_seconds
        if (not math.isfinite(base) or base <= 0 or
                not math.isfinite(max_backoff_seconds) or max_backoff_seconds < base):
            raise ValueError('backoff must be finite, positive, and capped at or above the base')
        self._clock = clock
        self._sleep = sleep_fn if sleep_fn is not None else (time.sleep if clock is time.monotonic else lambda seconds: None)
        self._max_backoff = max_backoff_seconds
        self._delay = 0.0
        self._retry_at = 0.0
        self._lock = threading.RLock()
        self._process_started_at = None
        self.last_runtime_status: dict | None = None

    def _call(self, operation):
        # Serialize this session's requests, including the recovery probe. No
        # SQLite transaction is held while waiting for the network.
        with self._lock:
            remaining = self._retry_at - self._clock()
            if remaining > 0:
                return {'available': False, 'reason': 'backoff',
                        'retry_after_seconds': remaining, 'uncertain': True}
            result = operation()
            if result['available']:
                self._delay = 0.0
                self._retry_at = 0.0
            else:
                delay = min(self._max_backoff, self._delay * 2
                            if self._delay else self.config.retry_interval_seconds)
                self._delay = delay
                self._retry_at = self._clock() + delay
                result = {**result, 'retry_after_seconds': delay}
            return result

    def detect_restart(self) -> bool:
        """True once when two valid health reads identify different processes.

        The first read returns False. Inspect last_runtime_status to distinguish
        unavailable/unsupported/backoff from a healthy unchanged process.
        An invalid response never erases the previous valid identity.
        """
        def read():
            info, error = client.get_runtime_info(
                base_url=self.config.paperclip_base_url,
                timeout=self.config.paperclip_timeout_seconds)
            if error:
                return {'available': False, 'reason': error}
            current = info['process_started_at']
            restarted = self._process_started_at is not None and current != self._process_started_at
            self._process_started_at = current
            return {'available': True, 'restarted': restarted}

        with self._lock:
            self.last_runtime_status = self._call(read)
            return self.last_runtime_status.get('restarted', False)

    def create_task_idempotent(self, company_id: str, title: str, description: str,
                               correlation_id: str, assignee_agent_id: str | None = None,
                               *, store: Store | None = None) -> dict:
        """Invoke #18 once when due; its durable uncertain-write policy remains intact."""
        return self._call(lambda: create_task_idempotent(
            company_id, title, description, correlation_id, assignee_agent_id,
            store=store, config=self.config, sleep_fn=self._sleep))

    def get_task_status(self, company_id: str, task_id: str, *, store: Store | None = None) -> dict:
        return self._call(lambda: get_task_status(
            company_id, task_id, config=self.config, store=store, now_fn=self._clock,
            sleep_fn=self._sleep,
        ))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paperclip_creations (
    operation_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    owner TEXT NOT NULL,
    result TEXT
);
"""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _error(reason: str, *, uncertain: bool = False) -> dict:
    return {'available': False, 'reason': reason, 'uncertain': uncertain}


def _success(remote: dict, *, cached: bool = False, reconciled: bool = False) -> dict:
    return {'available': True, 'task_id': remote['id'], 'task': remote,
            'cached': cached, 'reconciled': reconciled}


def _retryable_reason(reason: str | None) -> bool:
    if not isinstance(reason, str):
        return False
    lowered = reason.lower()
    if reason in {'timeout', 'offline', 'erro de rede'}:
        return True
    if 'erro no servidor do paperclip' in lowered:
        return True
    if 'paperclip recusou (5' in lowered:
        return True
    return False


def _fail_fast_reason(reason: str | None) -> bool:
    if not isinstance(reason, str):
        return False
    lowered = reason.lower()
    return (
        'autentica' in lowered
        or '(401)' in lowered
        or '(403)' in lowered
        or '(404)' in lowered
        or reason == 'not_found'
    )


def _with_retries(operation, *, sleep_fn: Callable[[float], None] | None = None,
                  retry_timeout: bool = True, retry_delays: tuple[float, ...] = _RETRY_DELAYS):
    sleep = sleep_fn or (lambda seconds: None)
    data, error = operation()
    for delay in retry_delays:
        if error is None or _fail_fast_reason(error) or not _retryable_reason(error):
            return data, error
        if error == 'timeout' and not retry_timeout:
            return data, error
        sleep(delay)
        data, error = operation()
    return data, error


def _find(company_id: str, operation_key: str, config: OrchestratorConfig,
          *, sleep_fn: Callable[[float], None] | None = None):
    tasks, error = _with_retries(
        lambda: client.list_company_tasks(
            company_id, query=operation_key,
            base_url=config.paperclip_base_url, timeout=config.paperclip_timeout_seconds,
        ),
        sleep_fn=sleep_fn,
    )
    if error:
        return None, error
    marker = f'<!-- jarvis-correlation:{operation_key} -->'
    matches = []
    for item in tasks:
        description = item.get('description')
        if description is not None and not isinstance(description, str):
            return None, 'invalid_task_description'
        if marker in (description or ''):
            matches.append(item)
    if len(matches) > 1:
        return None, 'ambiguous_correlation'
    return (matches[0] if matches else None), None


def _remember(store: Store, key: str, correlation_id: str, remote: dict) -> None:
    # Result is written first: a crash before the idempotency key is repaired
    # from this durable result on the next call, without another POST.
    store.execute('UPDATE paperclip_creations SET result = ? WHERE operation_key = ?',
                  (json.dumps(remote), key))
    store.record_idempotency_key(correlation_id, f'paperclip_task:{key}')


def create_task_idempotent(
    company_id: str, title: str, description: str, correlation_id: str,
    assignee_agent_id: str | None = None, *, store: Store | None = None,
    config: OrchestratorConfig | None = None, sleep_fn: Callable[[float], None] | None = None,
) -> dict:
    """Create once per server/company/correlation, with a durable task ID.

    Same key with different content is a conflict, not a second creation.
    Pass the runtime's Store; when omitted, a temporary connection to the normal
    orchestrator database is opened and closed. No network retry loop runs here.
    A pending claim with no remote match returns uncertain=True for reconciliation.
    """
    if not all(isinstance(value, str) and value.strip()
               for value in (company_id, title, correlation_id)):
        return _error('company_title_and_correlation_required')
    if not isinstance(description, str) or (assignee_agent_id is not None and not isinstance(assignee_agent_id, str)):
        return _error('invalid_task_input')
    cfg = config or load_config()
    key = _digest([cfg.paperclip_base_url.rstrip('/'), company_id, correlation_id])
    fingerprint = _digest([title, description, assignee_agent_id])
    owned = store is None
    try:
        store = store if store is not None else Store()
        store.ensure_schema(_SCHEMA)
        rows = store.query('SELECT fingerprint, owner, result FROM paperclip_creations WHERE operation_key = ?', (key,))
        has_key = store.has_idempotency_key(correlation_id, f'paperclip_task:{key}')
        if rows:
            if rows[0][0] != fingerprint:
                return _error('correlation_conflict')
            if rows[0][2] is not None:
                remote = json.loads(rows[0][2])
                if not isinstance(remote, dict) or not isinstance(remote.get('id'), str) or not remote['id']:
                    return _error('invalid_cached_task')
                if not has_key:
                    store.record_idempotency_key(correlation_id, f'paperclip_task:{key}')
                return _success(remote, cached=True)
        elif has_key:
            return _error('missing_cached_task', uncertain=True)

        # Read failure before claiming/sending is safe to retry. Read failure
        # after an earlier claim does not prove that its remote write failed.
        remote, error = _find(company_id, key, cfg, sleep_fn=sleep_fn)
        if error:
            return _error(error, uncertain=bool(rows))
        if rows:
            if remote is None:
                return _error('creation_unconfirmed', uncertain=True)
            _remember(store, key, correlation_id, remote)
            return _success(remote, reconciled=True)

        owner = str(uuid.uuid4())
        store.execute(
            'INSERT OR IGNORE INTO paperclip_creations (operation_key, fingerprint, owner) VALUES (?, ?, ?)',
            (key, fingerprint, owner),
        )
        claim = store.query('SELECT fingerprint, owner FROM paperclip_creations WHERE operation_key = ?', (key,))[0]
        if claim[0] != fingerprint:
            return _error('correlation_conflict')
        if claim[1] != owner:
            return _error('creation_in_progress', uncertain=True)
        if remote is not None:
            _remember(store, key, correlation_id, remote)
            return _success(remote, reconciled=True)

        remote, error = _with_retries(
            lambda: client.create_task(
                company_id, title, f'{description}\n\n<!-- jarvis-correlation:{key} -->',
                assignee_agent_id, base_url=cfg.paperclip_base_url,
                timeout=cfg.paperclip_timeout_seconds,
            ),
            sleep_fn=sleep_fn,
            # A POST timeout may mean Paperclip accepted the task and the
            # response was lost. Existing reconciliation will look up the
            # marker later; do not blindly replay that uncertain write.
            retry_timeout=False,
        )
        if error:
            return _error(error, uncertain=True)
        if not isinstance(remote, dict) or not isinstance(remote.get('id'), str) or not remote['id']:
            return _error('invalid_created_task', uncertain=True)
        _remember(store, key, correlation_id, remote)
        return _success(remote)
    except (sqlite3.Error, ValueError, TypeError):
        return _error('local_persistence_error', uncertain=True)
    finally:
        if owned and store is not None:
            store.close()


def find_created_task_id(
    company_id: str, correlation_id: str, *, store: Store, config: OrchestratorConfig | None = None,
) -> str | None:
    """Looks up the remote Paperclip task id already created for this
    (server, company_id, correlation_id) triple via a PAST
    `create_task_idempotent` call - never attempts a new creation, never
    makes a network call. Returns None whenever nothing was ever
    created (wrong/rotated server, never dispatched, or a lookup
    failure) so a caller (issue #157's sync tick) can call this freely
    on every locally-tracked task without special-casing "not
    dispatched yet" versus "dispatched but not found"."""
    if not all(isinstance(value, str) and value.strip() for value in (company_id, correlation_id)):
        return None
    cfg = config or load_config()
    key = _digest([cfg.paperclip_base_url.rstrip('/'), company_id, correlation_id])
    try:
        store.ensure_schema(_SCHEMA)
        rows = store.query('SELECT result FROM paperclip_creations WHERE operation_key = ?', (key,))
    except sqlite3.Error:
        return None
    if not rows or rows[0][0] is None:
        return None
    try:
        remote = json.loads(rows[0][0])
    except (ValueError, TypeError):
        return None
    task_id = remote.get('id') if isinstance(remote, dict) else None
    return task_id if isinstance(task_id, str) and task_id else None


def _status_from_task(company_id: str, task_id: str, item: dict) -> dict:
    status = item.get('status')
    if not isinstance(status, str) or not status:
        return _error('invalid_task_status')
    return {'available': True, 'task_id': task_id, 'status': status, 'task': item}


def _cached_status(task_id: str, entry, *, stale: bool = False, fallback_reason: str | None = None) -> dict:
    result = dict(entry.payload)
    result.update({
        'available': True,
        'task_id': task_id,
        'task': entry.payload,
        'status': entry.payload.get('status'),
        'cached': True,
        'stale': stale,
    })
    if fallback_reason is not None:
        result['fallback_reason'] = fallback_reason
    return result


def _audit_status_fallback(store: Store | None, result: str, *, company_id: str, task_id: str,
                           reason: str, cached: bool) -> None:
    if store is None:
        return
    try:
        audit_record(
            store, action='paperclip_status_fallback', origin='paperclip_ops', result=result,
            extra={'company_id': company_id, 'task_id': task_id, 'reason': reason, 'cached': cached},
        )
    except sqlite3.Error:
        pass


def get_task_status(company_id: str, task_id: str, *, config: OrchestratorConfig | None = None,
                    store: Store | None = None, now_fn: Callable[[], float] | None = None,
                    sleep_fn: Callable[[float], None] | None = None) -> dict:
    """Read an exact task ID through the company-scoped list endpoint."""
    if not all(isinstance(value, str) and value.strip() for value in (company_id, task_id)):
        return _error('company_and_task_required')
    cfg = config or load_config()
    cache = PaperclipCache(store) if store is not None else None
    now = (now_fn or time.monotonic)()
    cache_key = PaperclipCache.status_key(cfg.paperclip_base_url, company_id, task_id)
    cached = cache.get(cache_key, now) if cache is not None else None
    if cached is not None and cached.fresh:
        return _cached_status(task_id, cached)

    tasks, error = _with_retries(
        lambda: client.list_company_tasks(
            company_id, base_url=cfg.paperclip_base_url, timeout=cfg.paperclip_timeout_seconds,
        ),
        sleep_fn=sleep_fn,
        retry_delays=_RETRY_DELAYS if store is not None else (),
    )
    if error:
        if cached is not None and _retryable_reason(error):
            _audit_status_fallback(
                store, 'stale_cache_used', company_id=company_id, task_id=task_id,
                reason=error, cached=True,
            )
            return _cached_status(task_id, cached, stale=True, fallback_reason=error)
        _audit_status_fallback(
            store, 'no_cache_blocked', company_id=company_id, task_id=task_id,
            reason=error, cached=False,
        )
        return _error(error)
    for item in tasks:
        if item['id'] == task_id:
            result = _status_from_task(company_id, task_id, item)
            if result['available'] and cache is not None:
                cache.put(cache_key, item, now)
            return result
    return _error('not_found')
