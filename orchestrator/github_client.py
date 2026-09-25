"""GitHub issue client with a durable retry queue (#17).

Uses gh api with argument arrays and JSON stdin, never a shell. The runtime
calls retry_pending periodically; this module starts no worker or sleep loop.
A timed-out POST is reconciled by a remote body marker, never blindly replayed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit_in_transaction
from orchestrator.github_cache import GitHubCache
from orchestrator.persistence import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_github_ops (
    operation_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    owner TEXT,
    uncertain INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    issue_number INTEGER,
    issue_url TEXT
);
"""
_REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
_RATE_KEY = 'github:github.com:retry_not_before'
_HTTP_BLOCK = re.compile(r'HTTP/\d(?:\.\d)? (\d{3})[^\r\n]*\r?\n(?:[A-Za-z0-9-]+:[^\r\n]*\r?\n)*\r?\n')


class _Failure(Exception):
    def __init__(self, reason: str, *, uncertain: bool = False, retryable: bool = True,
                 retry_at: float | None = None):
        super().__init__(reason)
        self.reason, self.uncertain, self.retryable = reason, uncertain, retryable
        self.retry_at = retry_at


def _response_headers(stdout: str) -> tuple[str, list[tuple[int, dict[str, str]]]]:
    """Strip gh --include blocks, including those INSIDE a --slurp array.

    JSON strings escape newlines, so HTTP-looking user text cannot be a raw
    multiline header block. Keep every page's status; partial lists on failure
    must not masquerade as a complete deduplication result.
    """
    headers = []
    def remove(match):
        fields = {}
        for line in match[0].splitlines()[1:]:
            if ':' in line:
                name, value = line.split(':', 1)
                fields[name.lower()] = value.strip()
        headers.append((int(match[1]), fields))
        return ''
    return _HTTP_BLOCK.sub(remove, stdout), headers


def _server_retry_at(headers: dict[str, str], now: float) -> float:
    """Honor every known server floor; the latest applicable restriction wins."""
    deadlines = []
    retry_after = headers.get('retry-after', '')
    if retry_after.isdigit():
        delay = float(retry_after)
        if math.isfinite(delay) and delay > 0:
            deadlines.append(now + delay)
    elif retry_after:
        try:
            instant = parsedate_to_datetime(retry_after)
            deadline = instant.timestamp() if instant.utcoffset() is not None else 0
            if math.isfinite(deadline) and deadline > now:
                deadlines.append(deadline)
        except (ValueError, TypeError, OverflowError):
            pass
    if headers.get('x-ratelimit-remaining') == '0':
        try:
            deadline = float(headers.get('x-ratelimit-reset', ''))
            if math.isfinite(deadline) and deadline > now:
                deadlines.append(deadline)
        except ValueError:
            pass
    # GitHub recommends at least a minute for secondary limits with no
    # usable deadline. The operation's exponential backoff may extend this.
    return max(deadlines) if deadlines else now + 60


def _repo(value: str) -> str:
    if not isinstance(value, str) or not _REPO.fullmatch(value) or any(part in ('.', '..') for part in value.split('/')):
        raise ValueError('repo must use owner/name')
    return value.lower()


def _valid_issue(item) -> bool:
    return (isinstance(item, dict) and type(item.get('number')) is int and item['number'] > 0
            and isinstance(item.get('title'), str)
            and (item.get('body') is None or isinstance(item['body'], str)))


class GitHubClient:
    def __init__(self, store: Store, *, config: OrchestratorConfig | None = None,
                 run_fn: Callable | None = None, clock: Callable[[], datetime] | None = None,
                 timeout_seconds: float | None = None, max_backoff_seconds: float | None = None,
                 sleep_fn: Callable[[float], None] | None = None):
        self.store = store
        cfg = config or load_config()
        # #44: defaults come from config (GITHUB_TIMEOUT_SECONDS/
        # GITHUB_MAX_BACKOFF_SECONDS) rather than being hardcoded here -
        # an explicit caller-supplied value still always wins.
        timeout_seconds = timeout_seconds if timeout_seconds is not None else cfg.github_timeout_seconds
        max_backoff_seconds = max_backoff_seconds if max_backoff_seconds is not None else cfg.github_max_backoff_seconds
        for number in (cfg.retry_interval_seconds, timeout_seconds, max_backoff_seconds):
            if not math.isfinite(number) or number <= 0:
                raise ValueError('GitHub durations must be positive and finite')
        self.base_backoff = cfg.retry_interval_seconds
        self.max_backoff = max_backoff_seconds
        self.timeout = timeout_seconds
        self.lease = 2 * timeout_seconds + 30
        self.run = run_fn or subprocess.run
        self.sleep = sleep_fn if sleep_fn is not None else (lambda seconds: None if run_fn is not None else time.sleep(seconds))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cache = GitHubCache(store)
        store.ensure_schema(_SCHEMA)

    def _now(self) -> datetime:
        now = self.clock()
        if now.utcoffset() is None:
            raise ValueError('GitHub clock must be timezone-aware')
        return now.astimezone(timezone.utc)

    def _api(self, method: str, endpoint: str, payload: dict | None = None, *, paginate=False):
        if method != 'GET':
            return self._api_once(method, endpoint, payload, paginate=paginate)
        last_error = None
        for attempt in range(4):
            try:
                return self._api_once(
                    method, endpoint, payload, paginate=paginate, record_rate_limit=attempt == 3,
                )
            except _Failure as error:
                last_error = error
                if attempt == 3 or not self._retry_api_failure(error):
                    raise
                self.sleep(float(2 ** attempt))
        raise last_error

    @staticmethod
    def _retry_api_failure(error: _Failure) -> bool:
        if not error.retryable or error.reason == 'github_rate_limited':
            return False
        match = re.fullmatch(r'github_http_(\d{3})', error.reason)
        if match:
            status = int(match.group(1))
            return status == 429 or status == 408 or status >= 500
        return error.reason in {
            'timeout', 'process_unavailable', 'github_unavailable', 'invalid_github_response',
        }

    def _api_once(self, method: str, endpoint: str, payload: dict | None = None, *, paginate=False,
                  record_rate_limit=True):
        now = self._now().timestamp()
        stored_deadline = self.store.get_sync_value(_RATE_KEY)
        try:
            deadline = float(stored_deadline) if stored_deadline is not None else 0
        except ValueError:
            deadline = 0
        if math.isfinite(deadline) and deadline > now:
            raise _Failure('github_rate_limited', retry_at=deadline)
        args = ['gh', 'api', '--hostname', 'github.com', '--method', method, endpoint]
        args.append('--include')
        if paginate:
            args.extend(['--paginate', '--slurp'])
        if payload is not None:
            args.extend(['--input', '-'])
        try:
            result = self.run(
                args, input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=self.timeout, env={**os.environ, 'GH_PROMPT_DISABLED': '1'},
            )
        except FileNotFoundError:
            raise _Failure('gh_not_installed') from None
        except subprocess.TimeoutExpired:
            raise _Failure('timeout', uncertain=method == 'POST') from None
        except OSError:
            raise _Failure('process_unavailable') from None
        body, response_headers = _response_headers(result.stdout or '')
        failed_headers = [(status, fields) for status, fields in response_headers if status >= 400]
        if result.returncode != 0 or failed_headers:
            stderr = result.stderr or ''
            match = re.search(r'\bHTTP (\d{3})\b', stderr)
            status = failed_headers[-1][0] if failed_headers else int(match.group(1)) if match else None
            headers = failed_headers[-1][1] if failed_headers else {}
            retry_at = None
            if status == 429 or (status == 403 and ('retry-after' in headers or headers.get('x-ratelimit-remaining') == '0')):
                retry_at = _server_retry_at(headers, self._now().timestamp())
                # Shared across operations/client instances using this Store.
                # A shorter concurrent observation never shortens a cooldown.
                if record_rate_limit:
                    def record_limit(connection):
                        row = connection.execute('SELECT value FROM sync_state WHERE key=?', (_RATE_KEY,)).fetchone()
                        try:
                            previous = float(row[0]) if row else 0
                        except (ValueError, TypeError):
                            previous = 0
                        value = max(previous, retry_at) if math.isfinite(previous) else retry_at
                        connection.execute('INSERT INTO sync_state (key,value) VALUES (?,?) '
                                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (_RATE_KEY, str(value)))
                    self.store.run_in_transaction(record_limit)
            not_sent = any(text in stderr.lower() for text in ('no such host', 'connection refused', 'network is unreachable'))
            rejected = status is not None and 400 <= status < 500 and status != 408
            raise _Failure(
                f'github_http_{status}' if status else 'github_unavailable',
                uncertain=method == 'POST' and not (not_sent or rejected),
                retryable=status is None or status == 408 or status == 429 or status >= 500
                or (status == 403 and retry_at is not None),
                retry_at=retry_at,
            )
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            raise _Failure('invalid_github_response', uncertain=method == 'POST') from None

    def list_issues(self, repo: str, state: str = 'open', labels: list[str] | None = None) -> dict:
        try:
            repo = _repo(repo)
            if state not in ('open', 'closed', 'all'):
                raise ValueError('invalid state')
            if labels is not None and (not isinstance(labels, list) or any(not isinstance(label, str) for label in labels)):
                raise ValueError('invalid labels')
            params = {'state': state, 'per_page': 100}
            if labels:
                params['labels'] = ','.join(labels)
            now = self._now().timestamp()
            cache_key = GitHubCache.issue_list_key(repo, state, labels)
            cached = self.cache.get(cache_key, now)
            if cached is not None and cached.fresh:
                return {'available': True, 'issues': cached.payload, 'cached': True, 'stale': False}
            pages = self._api('GET', f'repos/{repo}/issues?{urlencode(params)}', paginate=True)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise _Failure('invalid_issue_list')
            issues = []
            for page in pages:
                for item in page:
                    if isinstance(item, dict) and 'pull_request' in item:
                        continue
                    if not _valid_issue(item):
                        raise _Failure('invalid_issue_list')
                    issues.append(item)
            self.cache.put(cache_key, issues, now)
            return {'available': True, 'issues': issues}
        except ValueError:
            return {'available': False, 'reason': 'invalid_list_input'}
        except _Failure as error:
            cached = self.cache.get(cache_key, self._now().timestamp()) if 'cache_key' in locals() else None
            if cached is not None and error.retryable:
                return {
                    'available': True, 'issues': cached.payload, 'cached': True, 'stale': True,
                    'fallback_reason': error.reason,
                }
            return {'available': False, 'reason': error.reason, 'retryable': error.retryable,
                    'retry_at': error.retry_at}
        except sqlite3.Error:
            return {'available': False, 'reason': 'local_persistence_error', 'retryable': True}

    def _row(self, key: str) -> dict:
        row = self.store.query(
            'SELECT payload, status, attempts, next_attempt, owner, uncertain, reason, issue_number, issue_url '
            'FROM pending_github_ops WHERE operation_key = ?', (key,),
        )[0]
        return dict(zip(('payload', 'status', 'attempts', 'next_attempt', 'owner', 'uncertain',
                         'reason', 'number', 'url'), row))

    def _result(self, key: str, *, cached=False) -> dict:
        row = self._row(key)
        if row['status'] == 'done':
            return {'available': True, 'number': row['number'], 'url': row['url'],
                    'operation_key': key, 'cached': cached}
        return {'available': False, 'reason': row['reason'] or 'pending',
                'pending': row['status'] != 'failed', 'uncertain': bool(row['uncertain']),
                'next_attempt': row['next_attempt'], 'operation_key': key}

    def create_issue(self, repo: str, title: str, body: str, labels: list[str], correlation_id: str) -> dict:
        try:
            repo = _repo(repo)
            if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
                raise ValueError('invalid content')
            if not isinstance(correlation_id, str) or not correlation_id.strip():
                raise ValueError('correlation required')
            if not isinstance(labels, list) or any(not isinstance(label, str) or not label.strip() for label in labels):
                raise ValueError('invalid labels')
            payload = json.dumps({'repo': repo, 'title': title, 'body': body,
                                  'labels': sorted(set(labels)), 'correlation_id': correlation_id}, sort_keys=True)
            key = hashlib.sha256(f'{repo}\0{correlation_id}'.encode('utf-8')).hexdigest()
            # Persist intent before attempting network work.
            self.store.execute(
                'INSERT OR IGNORE INTO pending_github_ops (operation_key, payload, next_attempt) VALUES (?, ?, ?)',
                (key, payload, self._now().timestamp()),
            )
            if self._row(key)['payload'] != payload:
                return {'available': False, 'reason': 'correlation_conflict', 'operation_key': key}
            return self._process(key)
        except ValueError:
            return {'available': False, 'reason': 'invalid_create_input'}
        except sqlite3.Error:
            return {'available': False, 'reason': 'local_persistence_error', 'pending': True}

    def update_issue_body(self, repo: str, issue_number: int, body: str) -> dict:
        """Replaces an existing Issue's body wholesale (#23's own need:
        backfilling BLOCKS once a dependent's number becomes known).
        Naturally idempotent - the caller always sends the full desired
        body, so repeating the same call is a no-op PATCH, not a
        duplicate write; no local bookkeeping needed here, unlike
        create_issue's dedup problem."""
        try:
            repo = _repo(repo)
            if type(issue_number) is not int or issue_number <= 0:
                raise ValueError('invalid issue number')
            if not isinstance(body, str):
                raise ValueError('invalid body')
            issue = self._api('PATCH', f'repos/{repo}/issues/{issue_number}', {'body': body})
            if not _valid_issue(issue):
                raise _Failure('invalid_updated_issue')
            self.cache.invalidate_issue_lists(repo)
            return {'available': True, 'number': issue['number']}
        except ValueError:
            return {'available': False, 'reason': 'invalid_update_input'}
        except _Failure as error:
            return {'available': False, 'reason': error.reason, 'retryable': error.retryable}

    def _defer(self, key: str, owner: str, error: _Failure, attempts: int) -> dict:
        delay = min(self.max_backoff, self.base_backoff * 2 ** min(attempts - 1, 30))
        next_attempt = max(self._now().timestamp() + delay, error.retry_at or 0)
        self.store.execute(
            "UPDATE pending_github_ops SET status = ?, next_attempt = ?, reason = ?, uncertain = ? "
            "WHERE operation_key = ? AND owner = ? AND status != 'done'",
            ('pending' if error.retryable else 'failed', next_attempt,
             error.reason, int(error.uncertain), key, owner),
        )
        return self._result(key)

    def _complete(self, key: str, issue: dict, *, record_creation: bool = True) -> dict:
        now = self._now()
        def complete(connection):
            payload_json, status = connection.execute(
                'SELECT payload, status FROM pending_github_ops WHERE operation_key = ?', (key,),
            ).fetchone()
            if status == 'done':
                return
            payload = json.loads(payload_json)
            url = f"https://github.com/{payload['repo']}/issues/{issue['number']}"
            connection.execute(
                "UPDATE pending_github_ops SET status = 'done', issue_number = ?, issue_url = ?, uncertain = 0, reason = NULL "
                'WHERE operation_key = ?', (issue['number'], url, key),
            )
            connection.execute(
                'INSERT OR IGNORE INTO idempotency_keys (correlation_id, kind, created_at) VALUES (?, ?, ?)',
                (payload['correlation_id'], f"github_issue:{payload['repo']}", now.isoformat()),
            )
            if record_creation:
                emit_in_transaction(
                    connection, EventType.TASK_CREATED,
                    {'repo': payload['repo'], 'issue_number': issue['number'], 'url': url, 'github_operation': key},
                    correlation_id=payload['correlation_id'], created_at=now,
                )
        self.store.run_in_transaction(complete)
        payload = json.loads(self._row(key)['payload'])
        self.cache.invalidate_issue_lists(payload['repo'])
        return self._result(key)

    def _process(self, key: str) -> dict:
        now, owner = self._now(), str(uuid.uuid4())
        def claim(connection):
            row = connection.execute(
                'SELECT status, next_attempt, uncertain, attempts FROM pending_github_ops WHERE operation_key = ?', (key,),
            ).fetchone()
            if row[0] in ('done', 'failed') or row[1] > now.timestamp():
                return None
            connection.execute(
                "UPDATE pending_github_ops SET status = 'reading', owner = ?, attempts = attempts + 1, next_attempt = ? "
                'WHERE operation_key = ?', (owner, now.timestamp() + self.lease, key),
            )
            return bool(row[2]), row[3] + 1
        claimed = self.store.run_in_transaction(claim)
        if claimed is None:
            return self._result(key, cached=True)
        uncertain, attempts = claimed
        payload = json.loads(self._row(key)['payload'])
        marker = f'<!-- jarvis-correlation:{key} -->'
        try:
            # A key without a completed local row cannot safely authorize another POST.
            if self.store.has_idempotency_key(payload['correlation_id'], f"github_issue:{payload['repo']}"):
                uncertain = True
            result = self.list_issues(payload['repo'], state='all')
            if not result['available']:
                raise _Failure(result['reason'], uncertain=uncertain, retryable=result.get('retryable', True),
                               retry_at=result.get('retry_at'))
            matches = [item for item in result['issues'] if marker in (item.get('body') or '')]
            matched_marker = bool(matches)
            if not matches and not uncertain:
                matches = [item for item in result['issues'] if item['title'].strip().casefold() == payload['title'].strip().casefold()]
            if len(matches) > 1:
                raise _Failure('ambiguous_duplicate', uncertain=uncertain, retryable=False)
            if matches:
                return self._complete(key, matches[0], record_creation=matched_marker)
            if uncertain:
                raise _Failure('creation_unconfirmed', uncertain=True)

            # Persist POST uncertainty before sending. A stale reading worker
            # cannot send if a newer owner took over its expired lease.
            self.store.execute(
                "UPDATE pending_github_ops SET status = 'posting', uncertain = 1 WHERE operation_key = ? AND owner = ? AND status = 'reading'",
                (key, owner),
            )
            row = self._row(key)
            if row['owner'] != owner or row['status'] != 'posting':
                return self._result(key)
            self.cache.invalidate_issue_lists(payload['repo'])
            issue = self._api('POST', f"repos/{payload['repo']}/issues", {
                'title': payload['title'], 'body': f"{payload['body']}\n\n{marker}", 'labels': payload['labels'],
            })
            if not _valid_issue(issue) or 'pull_request' in issue:
                raise _Failure('invalid_created_issue', uncertain=True)
            return self._complete(key, issue)
        except _Failure as error:
            return self._defer(key, owner, error, attempts)

    def retry_pending(self, limit: int = 20) -> list[dict]:
        """Process due operations once; backoff is persisted, never slept here."""
        if type(limit) is not int or limit <= 0:
            raise ValueError('limit must be a positive integer')
        keys = self.store.query(
            "SELECT operation_key FROM pending_github_ops WHERE status NOT IN ('done', 'failed') AND next_attempt <= ? "
            'ORDER BY next_attempt, operation_key LIMIT ?', (self._now().timestamp(), limit),
        )
        results = []
        for (key,) in keys:
            try:
                results.append(self._process(key))
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                results.append({'available': False, 'reason': 'local_persistence_error', 'operation_key': key, 'pending': True})
        return results

    def reprocess_pending_ops(self, limit: int = 20) -> list[dict]:
        """Startup hook: process due durable operations using the normal leases.

        Never clear rate-limit deadlines, completed rows or uncertain-write
        markers during restart. A live lease stays owned until it expires.
        Call again via the periodic retry path for operations not due yet.
        """
        return self.retry_pending(limit)


def reprocess_pending_ops(store: Store | None = None, *, limit: int = 20, **client_options) -> list[dict]:
    """Startup entrypoint for #30 without importing/starting the voice monolith."""
    owned = store is None
    active_store = store if store is not None else Store()
    try:
        return GitHubClient(active_store, **client_options).reprocess_pending_ops(limit)
    finally:
        if owned:
            active_store.close()
