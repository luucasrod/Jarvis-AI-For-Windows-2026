"""Daily scheduling with an injectable, timezone-aware clock (#25).

The runtime calls check_and_fire periodically and admit_task after planning.
No thread starts at import, no report content or CEO wakeup is implemented.
Missed ticks catch up only for today; a cycle missed past cutoff is recorded
without promoting tasks. Pending dependencies keep their queued state so #28
can reconsider them without clearing an explicit planner/human blocker.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.events import EventType, emit_in_transaction
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store


def _parse_time(value: str) -> time:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("Scheduler times must use HH:MM")
    return time.fromisoformat(value)


def _save_task(connection: sqlite3.Connection, task: Task) -> None:
    connection.execute(
        "INSERT INTO tasks (id, state, data) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET state=excluded.state, data=excluded.data",
        (task.id, task.state.value, json.dumps(task.to_dict())),
    )


def _dependencies_done(connection: sqlite3.Connection, task: Task) -> bool:
    for dependency in task.dependencies:
        row = connection.execute("SELECT state FROM tasks WHERE id = ?", (dependency,)).fetchone()
        if dependency == task.id or row is None or row[0] != TaskState.DONE.value:
            return False
    return True


class Scheduler:
    def __init__(
        self, store: Store, clock: Callable[[], datetime] | None = None,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config or load_config()
        self.tz = ZoneInfo(self.config.timezone)
        self.cycle_start = _parse_time(self.config.cycle_start_time)
        self.cutoff = _parse_time(self.config.cutoff_time)
        self.report_time = _parse_time(self.config.report_time)
        if not self.cycle_start < self.cutoff <= self.report_time:
            raise ValueError("Scheduler requires cycle_start < cutoff <= report_time on the same day")
        self.clock = clock or (lambda: datetime.now(self.tz))

    def _now(self, instant: datetime | None = None) -> datetime:
        instant = instant if instant is not None else self.clock()
        if instant.utcoffset() is None:
            raise ValueError("Scheduler clock must return a timezone-aware datetime")
        return instant.astimezone(self.tz)

    @staticmethod
    def _key(now: datetime, action: str) -> str:
        return f"scheduler:{now.date().isoformat()}:{action}"

    def _open(self, now: datetime, connection: sqlite3.Connection) -> bool:
        return self.cycle_start <= now.time() < self.cutoff and not connection.execute(
            "SELECT 1 FROM sync_state WHERE key = ?", (self._key(now, "cutoff"),)
        ).fetchone()

    def check_and_fire(self) -> list[str]:
        """Return names actually fired, once per local day, in schedule order."""
        now = self._now()
        fired = []
        for name, callback in (
            ("cycle_start", self.on_cycle_start),
            ("cutoff", self.on_cutoff),
            ("report_time", self.on_report_time),
        ):
            if callback(now):
                fired.append(name)
        return fired

    def on_cycle_start(self, instant: datetime | None = None) -> bool:
        now = self._now(instant)
        if now.time() < self.cycle_start:
            return False

        def promote(connection: sqlite3.Connection) -> None:
            if not self._open(now, connection):
                return
            rows = connection.execute(
                "SELECT data FROM tasks WHERE state = ?", (TaskState.NEXT_CYCLE.value,)
            ).fetchall()
            for row in rows:
                task = Task.from_dict(json.loads(row[0]))
                if not _dependencies_done(connection, task):
                    continue
                task.state = TaskState.READY
                task.updated_at = now.astimezone(timezone.utc)
                _save_task(connection, task)
                if task.state == TaskState.READY:
                    emit_in_transaction(
                        connection, EventType.TASK_READY, {"task_id": task.id},
                        task.correlation_id, task.project_id, created_at=now,
                    )

        return self.store.run_sync_once(self._key(now, "cycle_start"), now.isoformat(), promote)

    def on_cutoff(self, instant: datetime | None = None) -> bool:
        """Close admission for this day; never modify tasks already running."""
        now = self._now(instant)
        if now.time() < self.cutoff:
            return False
        return self.store.run_sync_once(self._key(now, "cutoff"), now.isoformat(), lambda _: None)

    def on_report_time(self, instant: datetime | None = None) -> bool:
        now = self._now(instant)
        if now.time() < self.report_time:
            return False
        return self.store.run_sync_once(
            self._key(now, "report_time"), now.isoformat(),
            lambda connection: emit_in_transaction(
                connection, EventType.REPORT_TIME_REACHED,
                {"date": now.date().isoformat(), "timezone": self.config.timezone,
                 "scheduled_time": self.config.report_time},
                correlation_id=self._key(now, "report_time"), created_at=now,
            ),
        )

    def admit_task(self, task: Task) -> Task:
        """Persist a planned task, applying cutoff even before the next tick.

        Existing tasks beyond PLANNED and human/security blockers are preserved.
        The returned copy is the persisted task; the input is not mutated.
        Before cycle start or at/after cutoff, PLANNED becomes NEXT_CYCLE.
        During the window it becomes READY only when all dependencies are DONE;
        otherwise it stays PLANNED for dependency reconsideration (#28).
        """
        now = self._now()

        def admit(connection: sqlite3.Connection) -> Task:
            row = connection.execute("SELECT data FROM tasks WHERE id = ?", (task.id,)).fetchone()
            stored = Task.from_dict(json.loads(row[0])) if row else Task.from_dict(task.to_dict())
            if stored.state == TaskState.PLANNED:
                if not self._open(now, connection):
                    stored.state = TaskState.NEXT_CYCLE
                else:
                    stored.state = TaskState.READY if _dependencies_done(connection, stored) else TaskState.PLANNED
                stored.updated_at = now.astimezone(timezone.utc)
                _save_task(connection, stored)
                if stored.state == TaskState.READY:
                    emit_in_transaction(
                        connection, EventType.TASK_READY, {"task_id": stored.id},
                        stored.correlation_id, stored.project_id, created_at=now,
                    )
            elif row is None:
                if stored.state not in (TaskState.BLOCKED, TaskState.NEEDS_LUCAS):
                    raise ValueError("admit_task expects a planned task or a planner blocker")
                _save_task(connection, stored)
            return stored

        return self.store.run_in_transaction(admit)

    def reconsider(self, task: Task, instant: datetime | None = None) -> Task:
        """Re-evaluates a single NEXT_CYCLE task mid-window, transactionally.

        `on_cycle_start` promotes every eligible NEXT_CYCLE task once per
        local day; a task whose dependency finishes LATER that same day
        (e.g. IN_PROGRESS at the 08:00 scan, DONE by 09:00) is never
        revisited by that one-shot guard and would otherwise wait until
        tomorrow (#30, Review Task #131 round 2). This exposes the exact
        same window/guard/dependency check for a caller (#30) to apply to
        one task at a time, without ever writing state directly: the
        current row is re-read fresh inside this transaction, and only a
        task that is (still) NEXT_CYCLE here is ever touched - a
        concurrent write that already moved it elsewhere is never
        overwritten.
        """
        now = self._now(instant)

        def apply(connection: sqlite3.Connection) -> Task:
            row = connection.execute("SELECT data FROM tasks WHERE id = ?", (task.id,)).fetchone()
            stored = Task.from_dict(json.loads(row[0])) if row else task
            if stored.state != TaskState.NEXT_CYCLE:
                return stored
            if self._open(now, connection) and _dependencies_done(connection, stored):
                stored.state = TaskState.READY
                stored.updated_at = now.astimezone(timezone.utc)
                _save_task(connection, stored)
                emit_in_transaction(
                    connection, EventType.TASK_READY, {"task_id": stored.id},
                    stored.correlation_id, stored.project_id, created_at=now,
                )
            return stored

        return self.store.run_in_transaction(apply)
