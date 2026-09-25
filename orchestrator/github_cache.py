"""SQLite-backed GitHub metadata cache for offline dispatch resilience."""
from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.persistence import Store

FRESH_TTL_SECONDS = 5 * 60
STALE_TTL_SECONDS = 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS github_api_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class CacheEntry:
    payload: object
    fetched_at: float
    age_seconds: float

    @property
    def fresh(self) -> bool:
        return self.age_seconds <= FRESH_TTL_SECONDS


class GitHubCache:
    def __init__(self, store: Store):
        self.store = store
        store.ensure_schema(_SCHEMA)

    @staticmethod
    def issue_list_key(repo: str, state: str, labels: list[str] | None) -> str:
        normalized_labels = ",".join(sorted(labels or []))
        return f"issues:{repo}:{state}:{normalized_labels}"

    def get(self, key: str, now: float) -> CacheEntry | None:
        rows = self.store.query(
            "SELECT payload, fetched_at FROM github_api_cache WHERE cache_key = ?", (key,)
        )
        if not rows:
            return None
        payload_json, fetched_at = rows[0]
        age = now - float(fetched_at)
        if age < 0 or age > STALE_TTL_SECONDS:
            return None
        return CacheEntry(json.loads(payload_json), float(fetched_at), age)

    def put(self, key: str, payload: object, fetched_at: float) -> None:
        self.store.execute(
            "INSERT INTO github_api_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (key, json.dumps(payload, ensure_ascii=False, sort_keys=True), fetched_at),
        )

    def invalidate_issue_lists(self, repo: str) -> None:
        self.store.execute("DELETE FROM github_api_cache WHERE cache_key LIKE ?", (f"issues:{repo}:%",))
