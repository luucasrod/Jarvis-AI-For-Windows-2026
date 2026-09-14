"""Scheduler acceptance, restart, transaction and Lisbon DST tests (#25)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.events import EventType, emit_in_transaction, query_events
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store
from orchestrator.scheduler import Scheduler

LISBON = ZoneInfo('Europe/Lisbon')


class Clock:
    def __init__(self, value='2026-09-12T07:59:00'):
        self.set(value)

    def set(self, value):
        self.value = datetime.fromisoformat(value).replace(tzinfo=LISBON)

    def __call__(self):
        return self.value


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / 'state.db')
    yield instance
    instance.close()


def task(state=TaskState.PLANNED, **kwargs):
    return Task(title='Task', objective='Objective', state=state, **kwargs)


def test_daily_schedule_and_admission(store):
    clock = Clock()
    scheduler = Scheduler(store, clock)
    pending = scheduler.admit_task(task())
    assert pending.state == TaskState.NEXT_CYCLE
    assert scheduler.check_and_fire() == []
    clock.set('2026-09-12T08:00:00')
    assert scheduler.check_and_fire() == ['cycle_start']
    assert scheduler.check_and_fire() == []
    assert store.get_task(pending.id).state == TaskState.READY
    clock.set('2026-09-12T13:59:00')
    assert scheduler.check_and_fire() == []
    assert scheduler.admit_task(task()).state == TaskState.READY
    running = task(TaskState.IN_PROGRESS)
    store.save_task(running)
    clock.set('2026-09-12T14:00:00')
    # Admission must honor cutoff even if no tick has run yet.
    assert scheduler.admit_task(task()).state == TaskState.NEXT_CYCLE
    assert scheduler.check_and_fire() == ['cutoff']
    assert scheduler.check_and_fire() == []
    assert store.get_task(running.id).to_dict() == running.to_dict()
    clock.set('2026-09-12T17:00:00')
    assert scheduler.check_and_fire() == ['report_time']
    assert scheduler.check_and_fire() == []
    reports = query_events(store, event_types=[EventType.REPORT_TIME_REACHED])
    assert len(reports) == 1
    assert reports[0]['payload']['date'] == '2026-09-12'
    assert reports[0]['created_at'] == datetime(2026, 9, 12, 16, tzinfo=timezone.utc)


def test_restart_and_next_day(tmp_path):
    path = tmp_path / 'state.db'
    clock = Clock('2026-09-12T08:00:00')
    first = Store(path)
    assert Scheduler(first, clock).check_and_fire() == ['cycle_start']
    first.close()
    second = Store(path)
    try:
        clock.set('2026-09-12T08:05:00')
        scheduler = Scheduler(second, clock)
        assert scheduler.check_and_fire() == []
        clock.set('2026-09-12T18:00:00')
        assert scheduler.check_and_fire() == ['cutoff', 'report_time']
        clock.set('2026-09-13T08:00:00')
        assert scheduler.check_and_fire() == ['cycle_start']
        clock.set('2026-09-12T18:00:00')
        assert scheduler.check_and_fire() == []
    finally:
        second.close()


def test_late_start_does_not_promote_after_cutoff(store):
    deferred = task(TaskState.NEXT_CYCLE)
    store.save_task(deferred)
    clock = Clock('2026-09-12T17:05:00')
    scheduler = Scheduler(store, clock)
    assert scheduler.check_and_fire() == ['cycle_start', 'cutoff', 'report_time']
    assert store.get_task(deferred.id).state == TaskState.NEXT_CYCLE
    clock.set('2026-09-13T08:00:00')
    assert scheduler.check_and_fire() == ['cycle_start']
    assert store.get_task(deferred.id).state == TaskState.READY


def test_dependency_checks_and_unlimited_cycle(store):
    done, unfinished = task(TaskState.DONE), task(TaskState.IN_PROGRESS)
    store.save_task(done)
    store.save_task(unfinished)
    ready = [task(TaskState.NEXT_CYCLE, dependencies=[done.id]) for _ in range(80)]
    missing = task(TaskState.NEXT_CYCLE, dependencies=['missing'])
    waiting = task(TaskState.NEXT_CYCLE, dependencies=[unfinished.id])
    self_ref = task(TaskState.NEXT_CYCLE)
    self_ref.dependencies = [self_ref.id]
    cycle_a, cycle_b = task(TaskState.NEXT_CYCLE), task(TaskState.NEXT_CYCLE)
    cycle_a.dependencies, cycle_b.dependencies = [cycle_b.id], [cycle_a.id]
    for item in ready + [missing, waiting, self_ref, cycle_a, cycle_b]:
        store.save_task(item)
    scheduler = Scheduler(store, Clock('2026-09-12T08:00:00'))
    scheduler.check_and_fire()
    assert len(store.list_tasks(TaskState.READY)) == 80
    for item in [missing, waiting, self_ref, cycle_a, cycle_b]:
        assert store.get_task(item.id).state == TaskState.NEXT_CYCLE
    assert len(query_events(store, event_types=[EventType.TASK_READY])) == 80
    assert scheduler.admit_task(task(dependencies=['missing'])).state == TaskState.PLANNED


@pytest.mark.parametrize('state', [TaskState.BLOCKED, TaskState.NEEDS_LUCAS])
def test_admission_preserves_planner_blockers(store, state):
    scheduler = Scheduler(store, Clock('2026-09-12T08:00:00'))
    blocked = task(state)
    assert scheduler.admit_task(blocked).to_dict() == blocked.to_dict()
    assert store.get_task(blocked.id).state == state
    assert query_events(store) == []


def test_admission_retry_preserves_current_task_and_emits_once(store):
    scheduler = Scheduler(store, Clock('2026-09-12T08:00:00'))
    planned = task()
    first = scheduler.admit_task(planned)
    assert planned.state == TaskState.PLANNED
    scheduler.admit_task(planned)
    assert len(query_events(store)) == 1
    first.state = TaskState.IN_PROGRESS
    store.save_task(first)
    assert scheduler.admit_task(planned).state == TaskState.IN_PROGRESS


def test_cutoff_guard_survives_clock_rollback(store):
    clock = Clock('2026-09-12T14:00:00')
    scheduler = Scheduler(store, clock)
    scheduler.check_and_fire()
    clock.set('2026-09-12T13:00:00')
    assert scheduler.admit_task(task()).state == TaskState.NEXT_CYCLE


@pytest.mark.parametrize('day,utc_hour', [('2026-03-28', 8), ('2026-03-29', 7),
                                         ('2026-10-24', 7), ('2026-10-25', 8)])
def test_lisbon_cycle_follows_dst(store, day, utc_hour):
    instant = datetime.fromisoformat(f'{day}T{utc_hour:02d}:00:00+00:00')
    scheduler = Scheduler(store, lambda: instant)
    assert scheduler.check_and_fire() == ['cycle_start']
    assert scheduler.check_and_fire() == []


def test_folded_hour_fires_once_and_skipped_hour_catches_up(store):
    config = OrchestratorConfig(cycle_start_time='00:00', cutoff_time='00:30', report_time='01:30')
    instant = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    scheduler = Scheduler(store, lambda: instant, config)
    assert scheduler.check_and_fire() == ['cycle_start', 'cutoff', 'report_time']
    instant = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert scheduler.check_and_fire() == []
    instant = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert scheduler.check_and_fire() == ['cycle_start', 'cutoff', 'report_time']


def test_custom_timezone_uses_local_date(store):
    config = OrchestratorConfig(timezone='Asia/Tokyo', cycle_start_time='00:30',
                                cutoff_time='01:00', report_time='02:00')
    scheduler = Scheduler(store, lambda: datetime(2026, 9, 12, 17, tzinfo=timezone.utc), config)
    assert scheduler.check_and_fire() == ['cycle_start', 'cutoff', 'report_time']
    assert query_events(store)[0]['payload']['date'] == '2026-09-13'


@pytest.mark.parametrize('settings', [{'cycle_start_time': '8:00'}, {'cutoff_time': '25:00'},
                                     {'cutoff_time': '07:00'}, {'report_time': '13:00'}])
def test_invalid_schedule_rejected(store, settings):
    with pytest.raises(ValueError):
        Scheduler(store, config=OrchestratorConfig(**settings))


def test_naive_clock_rejected(store):
    with pytest.raises(ValueError, match='timezone-aware'):
        Scheduler(store, lambda: datetime(2026, 9, 12, 8)).check_and_fire()


def test_report_and_guard_roll_back_together(store, monkeypatch):
    import orchestrator.scheduler as module
    scheduler = Scheduler(store, Clock('2026-09-12T17:00:00'))
    original = module.emit_in_transaction
    query_events(store)  # table preexists, rollback must also work in this case

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('simulated interruption before guard commit')

    monkeypatch.setattr(module, 'emit_in_transaction', fail_after_insert)
    with pytest.raises(RuntimeError):
        scheduler.on_report_time()
    assert query_events(store) == []
    assert store.get_sync_value('scheduler:2026-09-12:report_time') is None
    monkeypatch.setattr(module, 'emit_in_transaction', original)
    assert scheduler.on_report_time() is True
    assert scheduler.on_report_time() is False
    assert len(query_events(store)) == 1


def test_cycle_task_and_event_roll_back_together(store, monkeypatch):
    import orchestrator.scheduler as module
    deferred = task(TaskState.NEXT_CYCLE)
    store.save_task(deferred)
    scheduler = Scheduler(store, Clock('2026-09-12T08:00:00'))
    original = module.emit_in_transaction
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('simulated interruption after task update')
    monkeypatch.setattr(module, 'emit_in_transaction', fail)
    with pytest.raises(RuntimeError):
        scheduler.check_and_fire()
    assert store.get_task(deferred.id).state == TaskState.NEXT_CYCLE
    assert store.get_sync_value('scheduler:2026-09-12:cycle_start') is None
    assert query_events(store) == []
    monkeypatch.setattr(module, 'emit_in_transaction', original)
    assert scheduler.check_and_fire() == ['cycle_start']
    assert store.get_task(deferred.id).state == TaskState.READY


def test_concurrent_schedulers_across_connections_fire_once(tmp_path):
    stores = [Store(tmp_path / 'state.db') for _ in range(4)]
    barrier = Barrier(4)
    def fire(store):
        barrier.wait(timeout=5)
        return Scheduler(store, Clock('2026-09-12T17:00:00')).check_and_fire()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fire, stores))
        names = [name for result in results for name in result]
        assert sorted(names) == ['cutoff', 'cycle_start', 'report_time']
        assert len(query_events(stores[0])) == 1
    finally:
        for instance in stores:
            instance.close()


def test_dependency_completion_allows_readmission_without_clearing_blockers(store):
    scheduler = Scheduler(store, Clock('2026-09-12T09:00:00'))
    dependency = task(TaskState.IN_PROGRESS)
    store.save_task(dependency)
    waiting = task(dependencies=[dependency.id])
    assert scheduler.admit_task(waiting).state == TaskState.PLANNED
    dependency.state = TaskState.DONE
    store.save_task(dependency)
    assert scheduler.admit_task(waiting).state == TaskState.READY
    assert len(query_events(store, event_types=[EventType.TASK_READY])) == 1


def test_reconsider_promotes_next_cycle_once_its_dependency_finishes_same_day(store):
    # #30, Review Task #131 round 2: on_cycle_start's own scan only fires
    # once per day, so a NEXT_CYCLE child whose dependency finishes AFTER
    # that scan needs a way to be revisited mid-window - never a direct
    # state write.
    clock = Clock('2026-09-12T08:00:00')
    scheduler = Scheduler(store, clock)
    dependency = task(TaskState.IN_PROGRESS)
    store.save_task(dependency)
    child = task(TaskState.NEXT_CYCLE, dependencies=[dependency.id])
    store.save_task(child)

    assert scheduler.reconsider(child).state == TaskState.NEXT_CYCLE  # still IN_PROGRESS

    dependency.state = TaskState.DONE
    store.save_task(dependency)
    clock.set('2026-09-12T09:00:00')

    result = scheduler.reconsider(child)

    assert result.state == TaskState.READY
    assert len(query_events(store, event_types=[EventType.TASK_READY])) == 1


def test_reconsider_never_promotes_outside_the_window(store):
    scheduler = Scheduler(store, Clock('2026-09-12T16:00:00'))  # after cutoff
    child = task(TaskState.NEXT_CYCLE)
    store.save_task(child)

    result = scheduler.reconsider(child)

    assert result.state == TaskState.NEXT_CYCLE
    assert query_events(store, event_types=[EventType.TASK_READY]) == []


def test_reconsider_never_touches_a_task_that_is_no_longer_next_cycle(store):
    # A stale caller-held snapshot must never clobber concurrent state -
    # the current row (READY here, from some other path) is re-read fresh.
    scheduler = Scheduler(store, Clock('2026-09-12T09:00:00'))
    stale = task(TaskState.NEXT_CYCLE)
    store.save_task(stale)
    current = Task.from_dict(stale.to_dict())
    current.state = TaskState.READY
    store.save_task(current)

    result = scheduler.reconsider(stale)

    assert result.state == TaskState.READY
    assert query_events(store, event_types=[EventType.TASK_READY]) == []
