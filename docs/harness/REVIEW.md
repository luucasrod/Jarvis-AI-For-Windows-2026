# REVIEW.md - Independent critical review

Scope reviewed: `docs/harness/CURRENT_STATE.md`, `HARNESS_GAP_ANALYSIS.md`,
`TARGET_ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`, plus the referenced code in
`orchestrator/`, `docs/ai/WORK_PROTOCOL.md`, `docs/ai/CONTEXT.md`, and the
root `main.py` present in this worktree.

## ACCEPT

- ACCEPT: The broad "extend, do not recreate" thesis is correct. The code
  already has real scheduler admission/idempotency (`orchestrator/scheduler.py:46`,
  `:76`, `:136`, `:170`), planner decomposition/cycle blocking
  (`orchestrator/planner.py`), dependency eligibility/materialization
  (`orchestrator/task_queue.py:28`, `:463`), review attestation
  (`orchestrator/review_pipeline.py:94`), merge safety checks
  (`orchestrator/merge_policy.py:192`), and SQLite transaction primitives
  (`orchestrator/persistence.py:214`, `:224`, `:233`, `:248`). Rewriting these
  would be waste.

- ACCEPT: `HARNESS_GAP_ANALYSIS.md` is right that independent review is not a
  false gap. `request_review()` rejects non-concrete implementers/reviewers and
  rejects self-review (`orchestrator/review_pipeline.py:94` onwards).

- ACCEPT: `HARNESS_GAP_ANALYSIS.md` is right that merge serialization is still
  missing. `try_auto_merge()` validates one PR at a time and then calls
  `_merge_pr()` (`orchestrator/merge_policy.py:173`, `:192`, `:295`), with no
  repo-wide queue/claim around different PRs.

- ACCEPT: `HARNESS_GAP_ANALYSIS.md` is right that `healthcheck.py` diagnoses
  but does not recover. The module docstring explicitly says "no recovery,
  service startup or retry loop" (`orchestrator/healthcheck.py:1`), and
  `check_idle()` only returns causes such as `paperclip_unavailable` and
  `agent_paused` (`orchestrator/healthcheck.py:318`, `:428`, `:440`).

- ACCEPT: `CURRENT_STATE.md` is right that `WORK_PROTOCOL.md` is mostly
  operational convention, not mechanical enforcement. The protocol requires
  checkpoint branches, Review Tasks, and manual SOLO checks
  (`docs/ai/WORK_PROTOCOL.md:53`, `:75`, `:137`), but those are not enforced
  end-to-end by the scheduler/dispatcher.

## CORRECTION

- CORRECTION: `CURRENT_STATE.md` says `task_queue.py` owns "prioridade por
  rank+created_at". In the code, rank/created_at sorting is in
  `orchestrator/decisions.py:get_priority_queue()` (`orchestrator/decisions.py:292`,
  `:295`), not in `task_queue.py`. `task_queue.get_promotable_tasks()` only
  filters dependency eligibility in input order (`orchestrator/task_queue.py:28`,
  `:46`, `:48`). Fix the module attribution.

- CORRECTION: The claim that `voice_facade.py` is only imported by a forked
  `main.py` and not by code in this worktree is false for the checked-out
  source. Root `main.py` imports `orchestrator.voice_facade`
  (`main.py:52`) and calls `handle_status_query`, `handle_report_query`, and
  `handle_control_query` (`main.py:1663`, `:1672`, `:1681`). If the intended
  point is "the live production monolith process is another checkout and does
  not contain this wiring", say that explicitly and mark it as a runtime/deploy
  assertion, not a source-code assertion.

- CORRECTION: `HARNESS_GAP_ANALYSIS.md` G3 overstates "voz e orchestrator
  continuam desconectados" if judged against this worktree's source. The
  current source already has synchronous in-process voice facade calls in
  `main.py` (`main.py:52`, `:1663`, `:1672`, `:1681`). The real gap is narrower:
  the current coupling is in-process/import-time and not failure-isolated by an
  IPC boundary; and it may not be deployed in the live monolith checkout.

- CORRECTION: `TARGET_ARCHITECTURE.md` proposes SOLO enforcement in
  `task_queue.get_promotable_tasks`, but that function deliberately accepts only
  a snapshot list and does not mutate state or access `Store`
  (`orchestrator/task_queue.py:28`). The actual admission/dispatch path is
  `run_daily_cycle()` -> `get_priority_queue()` -> `Scheduler.admit_task()` /
  `Scheduler.reconsider()` -> dispatch (`orchestrator/orchestrator.py:407`,
  `:415`). SOLO conflict checks need to live in a transactional admission or
  dispatch reservation path, not only in this pure filter.

- CORRECTION: `TARGET_ARCHITECTURE.md` uses nonexistent task states
  `WORKING` and `REVIEW`. The enum names are `IN_PROGRESS` and `IN_REVIEW`
  (`orchestrator/models.py:16`, `:21`, `:22`). This is not cosmetic: a design
  written against the wrong states will produce either dead code or missed
  locks.

- CORRECTION: `TARGET_ARCHITECTURE.md` says implementing SOLO is simple because
  the `tasks` table has `state`. The table has `state`, but the current dispatch
  path does not set dispatched tasks to `IN_PROGRESS`; confirmed Paperclip
  assignment only appends IDs to return lists (`orchestrator/orchestrator.py:468`,
  `:476`), and `paperclip_sync.py` later jumps `READY`/`IN_PROGRESS` tasks
  directly to `DONE` when Paperclip is done (`orchestrator/paperclip_sync.py:66`,
  `:68`, `:104`). A SOLO lock based on active states will be ineffective until
  dispatch creates a durable active/lease state.

- CORRECTION: The proposed HTTP shape uses `GET /voice/control`. If "control"
  ever becomes real pause/resume, GET is the wrong method and is dangerous
  because crawlers, retries, or prefetch can trigger state changes. Use POST for
  control operations, keep GET only for status/report reads, and bind to
  loopback with a small shared local token or equivalent process-local trust
  check.

- CORRECTION: The merge queue schema in `TARGET_ARCHITECTURE.md` is underspecified.
  `merge_queue(pr_id, repo, requested_at, status, attempt)` does not bind the
  queued item to `task_id`, `expected_head_sha`, `expected_base`, required check
  policy, or a stale `MERGING` lease. `merge_policy.py` is intentionally strict
  about task/repo/PR/SHA/base (`orchestrator/merge_policy.py:70`, `:237`,
  `:279`, `:289`); the queue must preserve those invariants instead of queueing
  only a PR number.

## MISSING

- MISSING: Critical gap not listed: there is no durable local transition from
  `READY` to `IN_PROGRESS` when Paperclip work is actually assigned. The code
  tracks `READY` and `IN_PROGRESS` in sync (`orchestrator/paperclip_sync.py:66`,
  `:68`), and reports `IN_PROGRESS` in `run_report()` (`orchestrator/orchestrator.py:537`),
  but dispatch confirmation does not set that state (`orchestrator/orchestrator.py:468`,
  `:476`). This weakens progress reporting, idle diagnosis, crash recovery, and
  any future SOLO enforcement.

- MISSING: Control via voice is not merely disconnected; it is not implemented.
  `voice_facade.handle_control_query()` honestly replies that there is no real
  pause/resume mechanism (`orchestrator/voice_facade.py:182`, `:189`, `:191`).
  Adding an HTTP wrapper around the same function will not satisfy a
  status/report/control boundary unless the control semantics are either scoped
  out or implemented.

- MISSING: `IMPLEMENTATION_PLAN.md` should move the SOLO/active-state work
  earlier. Running Paperclip operationalization and voice integration before
  durable active-state + SOLO locking increases concurrent execution exactly in
  the area the plan says is risky. Recommended order: fix tests/token first;
  add durable dispatch active state/lease; enforce SOLO using that state; then
  make Paperclip auto-start/recover; then voice IPC; then merge queue and
  auto-merge gate.

- MISSING: `IMPLEMENTATION_PLAN.md` puts the auto-merge gate before Fase 5
  SOLO. That is backwards for an autonomous harness: broad auto-merge should
  not be considered operational until both merge serialization and resource
  serialization are enforced. Move Fase 5 before the auto-merge gate.

- MISSING: The plan should require tests for crash/restart around dispatch
  state, not only around merge. There are tests planned for "crash no meio do
  merge", but the more immediate harness failure mode is "Paperclip task
  created/assigned, local state still READY, process restarts, dispatcher
  repeats or misreports active work".

- MISSING: The documents do not call out that `WORK_PROTOCOL.md` requires Review
  Tasks as GitHub Issues (`docs/ai/WORK_PROTOCOL.md:75`, `:77`, `:83`), while
  `review_pipeline.py` records review verdict events but does not itself create
  or enforce those GitHub Review Tasks. That is a real convention-vs-enforcement
  gap separate from reviewer independence.

- MISSING: The HTTP boundary design should specify shared `Store` path/config
  explicitly. `Store()` defaults to a DB path derived from the checkout containing
  `orchestrator/persistence.py`; if voice and orchestrator run from different
  checkouts, they can silently read different SQLite files unless the HTTP server
  alone owns reads/writes or both processes are pinned to the same configured DB.
