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
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit_in_transaction
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


class _Failure(Exception):
    def __init__(self, reason: str, *, uncertain: bool = False, retryable: bool = True):
        super().__init__(reason)
        self.reason, self.uncertain, self.retryable = reason, uncertain, retryable


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
                 timeout_seconds: float = 30, max_backoff_seconds: float = 3600):
        self.store = store
        cfg = config or load_config()
        for number in (cfg.retry_interval_seconds, timeout_seconds, max_backoff_seconds):
            if not math.isfinite(number) or number <= 0:
                raise ValueError('GitHub durations must be positive and finite')
        self.base_backoff = cfg.retry_interval_seconds
        self.max_backoff = max_backoff_seconds
        self.timeout = timeout_seconds
        self.lease = 2 * timeout_seconds + 30
        self.run = run_fn or subprocess.run
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        store.ensure_schema(_SCHEMA)

    def _now(self) -> datetime:
        now = self.clock()
        if now.utcoffset() is None:
            raise ValueError('GitHub clock must be timezone-aware')
        return now.astimezone(timezone.utc)

    def _api(self, method: str, endpoint: str, payload: dict | None = None, *, paginate=False):
        args = ['gh', 'api', '--hostname', 'github.com', '--method', method, endpoint]
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
        if result.returncode != 0:
            stderr = result.stderr or ''
            match = re.search(r'\bHTTP (\d{3})\b', stderr)
            status = int(match.group(1)) if match else None
            not_sent = any(text in stderr.lower() for text in ('no such host', 'connection refused', 'network is unreachable'))
            rejected = status is not None and 400 <= status < 500 and status != 408
            raise _Failure(
                f'github_http_{status}' if status else 'github_unavailable',
                uncertain=method == 'POST' and not (not_sent or rejected),
                retryable=status not in (400, 404, 422),
            )
        try:
            return json.loads(result.stdout)
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
            return {'available': True, 'issues': issues}
        except ValueError:
            return {'available': False, 'reason': 'invalid_list_input'}
        except _Failure as error:
            return {'available': False, 'reason': error.reason, 'retryable': error.retryable}

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
            return {'available': True, 'number': issue['number']}
        except ValueError:
            return {'available': False, 'reason': 'invalid_update_input'}
        except _Failure as error:
            return {'available': False, 'reason': error.reason, 'retryable': error.retryable}

    def _defer(self, key: str, owner: str, error: _Failure, attempts: int) -> dict:
        delay = min(self.max_backoff, self.base_backoff * 2 ** min(attempts - 1, 30))
        self.store.execute(
            "UPDATE pending_github_ops SET status = ?, next_attempt = ?, reason = ?, uncertain = ? "
            "WHERE operation_key = ? AND owner = ? AND status != 'done'",
            ('pending' if error.retryable else 'failed', self._now().timestamp() + delay,
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
                raise _Failure(result['reason'], uncertain=uncertain, retryable=result.get('retryable', True))
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
