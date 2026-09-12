# Scheduler integration (#25)

Create one `Scheduler(store, clock=None, config=None)` alongside the runtime.
Call `check_and_fire()` periodically; it starts no thread and performs no network
requests. Call `admit_task(task)` for each task returned by the planner, and use
its returned persisted copy. The input object is not mutated. The runtime wiring
is #30; blocked dependency reconsideration is #28; report formatting is #32.

- Times use config's IANA timezone and HH:MM settings. Defaults are Lisbon,
  08:00 / 14:00 / 17:00. Require start < cutoff <= report within one local day.
- A supplied clock must return an aware datetime; UTC clocks are converted to
  the configured zone. Naive clocks and invalid schedules are rejected early.
- Before start and at/after cutoff, new PLANNED tasks become NEXT_CYCLE. Inside
  the window they become READY only if every dependency exists and is DONE;
  otherwise BLOCKED. Planner BLOCKED/NEEDS_LUCAS tasks stay blocked. Existing
  tasks past PLANNED are not reset by repeated admission.
- At cycle start every NEXT_CYCLE task is evaluated, with no batch cap. A pending,
  missing, self or cyclic dependency prevents READY. Only DONE satisfies a dependency.
- Missed polling ticks catch up today's actions. Starting after cutoff records
  the missed cycle without promoting tasks. No historical days are replayed.
- A completed cutoff remains closed if the clock moves backward within that day.
  Per-date sync keys prevent duplicate dispatch across restarts and DST folds.
  A skipped local schedule time fires at the first poll after it.
- Report time only appends `REPORT_TIME_REACHED` with local date, timezone and
  scheduled time. The event timestamp uses the injected instant normalized to UTC.

`Store.run_in_transaction` and `run_sync_once` are additive SQL transaction APIs.
Scheduler state, emitted events and sync guards commit together. Exceptions roll
back all effects and leave the occurrence retryable. SQLite BEGIN IMMEDIATE also
serializes independent connections. Callbacks must use the supplied connection,
without commits, executescript, other Store methods or network I/O. Existing Store
APIs retain their behavior. `events.emit_in_transaction` does not commit, unlike
ordinary `emit`; use it only within the transaction callback.

The exactly-once guarantee covers local SQLite effects. Delivery of a report,
Paperclip wakeups and other external side effects need their own idempotency in
the consuming integration. No background service is started by this change.

Windows needs an IANA database; `tzdata` is declared as a dependency as recommended
by the [Python zoneinfo documentation](https://docs.python.org/3/library/zoneinfo.html#data-sources).
Tests use injected clocks (including Lisbon spring/autumn transitions), restart,
concurrent connections and forced failures after writes; they never wait for a
real scheduled hour.
