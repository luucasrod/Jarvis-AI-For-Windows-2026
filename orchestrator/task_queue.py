"""Dependency-local queue eligibility (#28), without global blocking.

Call get_promotable_tasks with a fresh Store.list_tasks() snapshot whenever a
task completes or a blocker is resolved. This function never changes states:
a dependency wait only excludes a task from the returned list, so it can become
eligible on the next snapshot without clearing a planner/human BLOCKED state.
The scheduler/runtime still applies cycle, cutoff and execution-agent policy.

Also holds materialize_plan (#23): the bridge from a planner.PlanResult to
the project's REAL task queue.
"""
from __future__ import annotations

import re
from pathlib import Path

from datetime import datetime, timezone

from orchestrator.audit import query_audit
from orchestrator.audit import record as audit_record
from orchestrator.github_client import GitHubClient
from orchestrator.models import Task, TaskState
from orchestrator.persistence import Store

_QUEUED_STATES = {TaskState.PLANNED, TaskState.NEXT_CYCLE, TaskState.READY}


def get_promotable_tasks(all_tasks: list[Task]) -> list[Task]:
    """Return queued tasks whose own dependencies are all DONE, in input order.

    Missing, self and cyclic dependencies cannot satisfy the DONE requirement.
    A blocked ancestor keeps its queued descendants unavailable transitively:
    each descendant waits for its immediate predecessors to actually finish.
    Unblocking an ancestor alone does not count as completing it.

    Explicit BLOCKED/NEEDS_LUCAS, unplanned, active and terminal tasks are never
    returned. Duplicate IDs are rejected rather than choosing an arbitrary
    state that could incorrectly permit execution. The input is not mutated.
    """
    by_id: dict[str, Task] = {}
    for task in all_tasks:
        if task.id in by_id:
            raise ValueError(f"Duplicate task id in queue snapshot: {task.id}")
        by_id[task.id] = task

    return [
        task for task in all_tasks
        if task.state in _QUEUED_STATES
        and all(
            dependency != task.id
            and dependency in by_id
            and by_id[dependency].state == TaskState.DONE
            for dependency in task.dependencies
        )
    ]


_FALLBACK_PATH_PATTERN = re.compile(r"[\w./\\-]+\.(?:md|txt)", re.IGNORECASE)
_GITHUB_ISSUES_MENTION = re.compile(r"github[ _]issues", re.IGNORECASE)
# task_source is free, human-written prose (see project_context_index.json) -
# it can mention "GitHub Issues" while actively denying it applies here, e.g.
# "... Not GitHub Issues for this project." A same-sentence "not" nearby must
# override a bare substring match, or that sentence gets read backwards.
_GITHUB_ISSUES_NEGATED = re.compile(r"\bnot\b[^.]{0,30}github[ _]issues", re.IGNORECASE)


def _uses_github_issues(project_context) -> bool:
    """A resolvable project-owned queue file (e.g. FILA.md) is concrete
    evidence of the actual convention and always wins over a free-text
    mention of GitHub Issues, negated or not. Absent that, only an
    unnegated "GitHub Issues" mention counts."""
    source = project_context.task_source or ""
    if not source:
        return False
    if _resolve_fallback_path(project_context) is not None:
        return False
    if not _GITHUB_ISSUES_MENTION.search(source):
        return False
    return not _GITHUB_ISSUES_NEGATED.search(source)


def _resolve_fallback_path(project_context) -> Path | None:
    """Best-effort extraction of the project's own queue file from its
    (free-text, human-written) task_source description - e.g. 'docs\\ai\\
    FILA.md in this repo'. Returns None when no such path is found, OR
    when the extracted path would resolve outside project_context.root
    (a rooted/absolute fragment like '/etc/x.md', or a '../' escape) -
    this module never guesses a filename NOR writes anywhere the project
    didn't authorize, per #23's OUT OF SCOPE (never forces a migration/
    format the project didn't already establish, and never as a side
    door out of its own directory)."""
    if not project_context.root or not project_context.task_source:
        return None
    match = _FALLBACK_PATH_PATTERN.search(project_context.task_source)
    if not match:
        return None
    root = Path(project_context.root).resolve()
    candidate = (root / match.group(0).replace("\\", "/")).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _analyze_batch(tasks: list[Task], store: Store) -> tuple[list[Task], set[str]]:
    """Topologically orders `tasks` (Kahn's algorithm, dependencies within
    this batch only) and computes which ids must be DEFERRED - never
    materialized as free-standing work in either destination - because
    they are NEEDS_LUCAS, already BLOCKED (including a cycle the planner
    already flagged, #22), part of a cycle within this batch that ISN'T
    state-flagged (defensive - a hand-built plan might not have gone
    through the planner's own cycle detection), depend on a dependency
    OUTSIDE this batch that isn't verifiably DONE in the Store (Review
    Task #117 round 2, finding #1: the planner's own dedup can point a
    dependency at a persisted task that hasn't finished yet - "not in
    this batch" never means "already resolved", only real evidence does;
    an external dependency that IS confirmed DONE is fine to proceed
    past, it just can't be cited by a real Issue number here), or depend
    directly or transitively on another deferred task in this same
    batch."""
    by_id = {task.id: task for task in tasks}
    remaining_deps = {
        task.id: sum(1 for dep_id in task.dependencies if dep_id in by_id)
        for task in tasks
    }
    ready = [task for task in tasks if remaining_deps[task.id] == 0]
    ordered: list[Task] = []
    resolved: set[str] = set()

    while ready:
        task = ready.pop(0)
        if task.id in resolved:
            continue
        resolved.add(task.id)
        ordered.append(task)
        for other in tasks:
            if other.id in resolved or task.id not in other.dependencies:
                continue
            remaining_deps[other.id] -= 1
            if remaining_deps[other.id] == 0:
                ready.append(other)

    cyclic = [task for task in tasks if task.id not in resolved]
    ordered.extend(cyclic)

    deferred = {task.id for task in tasks if task.state in (TaskState.NEEDS_LUCAS, TaskState.BLOCKED)}
    deferred.update(task.id for task in cyclic)

    for task in tasks:
        if task.id in deferred:
            continue
        for dep_id in task.dependencies:
            if dep_id in by_id:
                continue  # in-batch - handled by the transitive closure below
            existing = store.get_task(dep_id)
            if existing is None or existing.state != TaskState.DONE:
                deferred.add(task.id)
                break

    changed = True
    while changed:
        changed = False
        for task in tasks:
            if task.id in deferred:
                continue
            if any(dep_id in by_id and dep_id in deferred for dep_id in task.dependencies):
                deferred.add(task.id)
                changed = True

    return ordered, deferred


_RELATIONSHIPS_SECTION = re.compile(
    r"\n?## RELATIONSHIPS\n(?:BLOCKED_BY:.*\n?)?(?:BLOCKS:.*\n?)?", re.IGNORECASE,
)
_RELATIONSHIP_LINE = re.compile(r"^(BLOCKED_BY|BLOCKS):\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ISSUE_NUMBER_REF = re.compile(r"#(\d+)")


def _relationships_block(blocked_by_numbers: list[int], blocks_numbers: list[int]) -> str:
    return (
        "## RELATIONSHIPS\n"
        "BLOCKED_BY: " + (", ".join(f"#{n}" for n in blocked_by_numbers) if blocked_by_numbers else "none") + "\n"
        "BLOCKS: " + (", ".join(f"#{n}" for n in blocks_numbers) if blocks_numbers else "none") + "\n"
    )


def _issue_body(task: Task, blocked_by_numbers: list[int], blocks_numbers: list[int]) -> str:
    lines = [task.objective or task.title, ""]
    if task.acceptance_criteria:
        lines.append("## ACCEPTANCE CRITERIA")
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
        lines.append("")
    lines.append(_relationships_block(blocked_by_numbers, blocks_numbers).rstrip("\n"))
    return "\n".join(lines)


def _existing_relationship_numbers(existing_body: str) -> tuple[list[int], list[int]]:
    """Parses whatever BLOCKED_BY/BLOCKS numbers are already in the
    Issue's CURRENT body - independent of this batch's own bookkeeping,
    so a relationship this batch knows nothing about (a human edit, a
    reference from a completely different plan) is never silently lost."""
    section = _RELATIONSHIPS_SECTION.search(existing_body)
    if not section:
        return [], []
    blocked_by: list[int] = []
    blocks: list[int] = []
    for line_match in _RELATIONSHIP_LINE.finditer(section.group(0)):
        numbers = [int(n) for n in _ISSUE_NUMBER_REF.findall(line_match.group(2))]
        if line_match.group(1).upper() == "BLOCKED_BY":
            blocked_by = numbers
        else:
            blocks = numbers
    return blocked_by, blocks


def _patch_relationships(existing_body: str, new_blocks_numbers: list[int]) -> str:
    """Adds `new_blocks_numbers` to the Issue's CURRENT BLOCKS list -
    never a wholesale rewrite of the RELATIONSHIPS section. The first two
    versions of this backfill each destroyed something outside this
    batch's own knowledge: v1 regenerated the whole body (erasing #17's
    correlation marker and any human content, round 2 finding #3); v2
    preserved the rest of the body but still overwrote BOTH relationship
    lines from this batch's OWN bookkeeping alone, discarding a
    pre-existing BLOCKED_BY/BLOCKS this batch has no knowledge of - e.g.
    a parent Issue #17 deduplicated by title, or one a human edited after
    creation (Review Task #117 round 3, finding #2). BLOCKED_BY here is
    therefore left exactly as already written (never recomputed by a
    BLOCKS-only backfill), and the new BLOCKS numbers are UNIONED with
    whatever was already listed, never replacing it. A body with no
    recognizable RELATIONSHIPS section (unexpected shape - a human-
    authored issue, say) gets the section appended rather than risk
    rewriting content whose structure isn't understood."""
    existing_blocked_by, existing_blocks = _existing_relationship_numbers(existing_body)
    merged_blocks = sorted(set(existing_blocks) | set(new_blocks_numbers))
    new_section = _relationships_block(existing_blocked_by, merged_blocks)
    if _RELATIONSHIPS_SECTION.search(existing_body):
        return _RELATIONSHIPS_SECTION.sub("\n" + new_section, existing_body, count=1)
    separator = "" if not existing_body else ("\n\n" if not existing_body.endswith("\n\n") else "")
    return existing_body + separator + new_section


def _materialize_to_github(
    tasks: list[Task], project_context, store: Store, client: GitHubClient | None,
) -> list[int]:
    if not project_context.repository:
        return []
    client = client or GitHubClient(store)
    by_id = {task.id: task for task in tasks}
    ordered, deferred = _analyze_batch(tasks, store)
    task_to_issue: dict[str, int] = {}
    created: list[int] = []

    for task in ordered:
        if task.id in deferred:
            continue
        unresolved_in_batch = [
            dep_id for dep_id in task.dependencies
            if dep_id in by_id and dep_id not in task_to_issue
        ]
        if unresolved_in_batch:
            # A dependency in THIS batch hasn't (yet, or ever) become a
            # real Issue - publishing this task now would show it as
            # free-standing (BLOCKED_BY: none) when it is not. Deferring
            # it also makes it unresolved for anything depending on IT,
            # cascading forward through however many levels apply.
            deferred.add(task.id)
            continue
        blocked_by_numbers = [task_to_issue[dep_id] for dep_id in task.dependencies if dep_id in task_to_issue]
        result = client.create_issue(
            project_context.repository, task.title, _issue_body(task, blocked_by_numbers, []),
            ["origin:planner"], task.correlation_id,
        )
        if result.get("available") and isinstance(result.get("number"), int):
            task_to_issue[task.id] = result["number"]
            created.append(result["number"])
        else:
            deferred.add(task.id)

    # Second pass: a child's Issue number cannot be known before it
    # exists, so BLOCKS can only be backfilled onto its parent's body
    # after every creation in this batch has settled. This patches only
    # the RELATIONSHIPS section of the parent's CURRENT remote body (see
    # _patch_relationships) - never a wholesale regeneration, which used
    # to erase #17's own correlation marker and any human-added content
    # (Review Task #117 round 2, finding #3).
    blocks_by_parent: dict[str, list[int]] = {}
    for task in tasks:
        child_number = task_to_issue.get(task.id)
        if child_number is None:
            continue
        for dep_id in task.dependencies:
            parent_number = task_to_issue.get(dep_id)
            if parent_number is not None:
                blocks_by_parent.setdefault(dep_id, []).append(child_number)

    if blocks_by_parent:
        fetch = client.list_issues(project_context.repository, state="all")
        current_by_number = {item["number"]: item for item in fetch["issues"]} if fetch.get("available") else {}

        for parent_id, blocks_numbers in blocks_by_parent.items():
            parent = by_id[parent_id]
            parent_number = task_to_issue[parent_id]
            current = current_by_number.get(parent_number)
            if current is None:
                # Can't safely patch without reading the real current body
                # first - recorded as a queryable incomplete relationship
                # (see list_incomplete_relationships) rather than silently
                # guessed at or overwritten blind.
                _record_incomplete_relationship(store, project_context.repository, parent, parent_number,
                                                "current_body_unavailable")
                continue
            new_body = _patch_relationships(current.get("body") or "", sorted(blocks_numbers))
            result = client.update_issue_body(project_context.repository, parent_number, new_body)
            if not result.get("available"):
                _record_incomplete_relationship(store, project_context.repository, parent, parent_number,
                                                result.get("reason"))

    return created


_INCOMPLETE_RELATIONSHIP_ACTION = "materialize_relationships"


def _record_incomplete_relationship(store: Store, repo: str, parent: Task, issue_number: int, reason) -> None:
    audit_record(
        store, action=_INCOMPLETE_RELATIONSHIP_ACTION, origin="task_queue", result="incomplete",
        correlation_id=parent.correlation_id, project_id=parent.project_id,
        extra={"repo": repo, "issue_number": issue_number, "reason": reason},
    )


def list_incomplete_relationships(store: Store, repo: str | None = None) -> list[dict]:
    """Returns every BLOCKS backfill that never completed (Review Task
    #117 round 3, finding #3: a log entry alone is evidence, not a
    contract a caller can act on) - a durable, queryable record of
    exactly which (repo, issue_number, correlation_id, reason) still
    needs its relationships repaired, for a future retry pass to consume.
    This module doesn't retry automatically - no runtime currently calls
    materialize_plan on a recurring schedule that could safely re-run
    just the repair - but the record is real and actionable, not merely
    logged prose."""
    entries = query_audit(store, project_id=None)
    incomplete = [
        {
            "repo": entry["extra"].get("repo"),
            "issue_number": entry["extra"].get("issue_number"),
            "correlation_id": entry["correlation_id"],
            "reason": entry["extra"].get("reason"),
            "created_at": entry["created_at"],
        }
        for entry in entries
        if entry["action"] == _INCOMPLETE_RELATIONSHIP_ACTION and entry["result"] == "incomplete"
    ]
    if repo is not None:
        incomplete = [item for item in incomplete if item["repo"] == repo]
    return incomplete


def _fallback_marker(correlation_id: str) -> str:
    return f"<!-- jarvis-correlation:{correlation_id} -->"


def _format_fallback_block(pending: list[Task], by_id: dict[str, Task]) -> str:
    lines = ["## Novas tarefas do planner", ""]
    for task in pending:
        dep_titles = [by_id[dep_id].title for dep_id in task.dependencies if dep_id in by_id]
        lines.append(
            f"- [ ] {task.title} (depende de: {', '.join(dep_titles) if dep_titles else 'nenhuma'}) "
            f"{_fallback_marker(task.correlation_id)}"
        )
        if task.objective and task.objective != task.title:
            lines.append(f"  {task.objective}")
    return "\n".join(lines) + "\n"


def _materialize_to_fallback(tasks: list[Task], path: Path, store: Store) -> None:
    """Appends never-before-written tasks to the project's own queue file.

    Each written line carries a `jarvis-correlation` marker (same idea as
    #17's own Issue-body marker) - the authority a replay checks, not
    just #13's idempotency_keys table (kept as a secondary, faster
    guard). Re-scanning the file's actual text (rather than trusting the
    DB key alone) closes the crash window between a successful append and
    the key being recorded (Review Task #117 round 2, finding #2).

    The whole read-check-append-record sequence runs inside one
    `run_in_transaction`: SQLite's BEGIN IMMEDIATE takes a real write lock
    that blocks a SECOND connection's own call to this function until the
    first one commits - a plain re-read without that lock left a window
    where two concurrent writers could both see "not written yet" and
    both append, duplicating the entry (Review Task #117 round 3, finding
    #1, reproduced with two coordinated connections). The lock only
    covers the DURATION of this callback, not the file's own atomicity
    guarantees - two Jarvis processes/threads sharing the same Store
    (the only writers this codebase has) are correctly serialized; an
    entirely separate, uncoordinated process writing to the same path
    outside this module is not this function's concern.
    """
    by_id = {task.id: task for task in tasks}
    ordered, deferred = _analyze_batch(tasks, store)
    kind = f"fallback_queue:{path}"
    candidates = [task for task in ordered if task.id not in deferred]
    if not candidates:
        return

    def apply(connection) -> None:
        try:
            existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            return

        pending = [
            task for task in candidates
            if _fallback_marker(task.correlation_id) not in existing_text
            and not connection.execute(
                "SELECT 1 FROM idempotency_keys WHERE correlation_id = ? AND kind = ?",
                (task.correlation_id, kind),
            ).fetchone()
        ]
        if not pending:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n" + _format_fallback_block(pending, by_id))
        except OSError:
            return
        now = datetime.now(timezone.utc).isoformat()
        for task in pending:
            connection.execute(
                "INSERT OR IGNORE INTO idempotency_keys (correlation_id, kind, created_at) VALUES (?, ?, ?)",
                (task.correlation_id, kind, now),
            )

    store.run_in_transaction(apply)


def materialize_plan(
    plan_result, project_context, store: Store, *, client: GitHubClient | None = None,
) -> list[int]:
    """Turns plan_result.tasks into the project's REAL task queue.

    Returns the GitHub Issue numbers created (in topological/creation
    order) when `project_context.task_source` indicates GitHub Issues is
    this project's queue (section 13). Each Issue's body records its
    BLOCKED_BY as real Issue numbers known at creation time, and gets its
    BLOCKS backfilled in a second pass once every creation in the batch
    has settled. Idempotency for a replay of the same plan comes from
    passing each Task's own stable correlation_id through to #17's
    GitHubClient.create_issue, which already dedupes on (repo,
    correlation_id) - this module adds none of its own for that part.

    A task that is NEEDS_LUCAS, already BLOCKED, part of an unresolved
    cycle within this batch, or depends (directly or transitively) on
    another deferred task is never materialized as if it were free
    (see _analyze_batch) - it is simply left for a later call once its
    blocker resolves, in EITHER destination.

    When the project does NOT use GitHub Issues, this never forces that
    convention on it (section 13): it appends a generic formatted text
    block to the project's own queue file, resolved from task_source (and
    verified to stay within project_context.root - see
    _resolve_fallback_path), and always returns `[]` (there is nothing to
    report as an Issue number). A project whose queue file can't be
    resolved from task_source simply gets no fallback write - migrating
    it onto a new format is out of scope.
    """
    tasks = list(plan_result.tasks)
    if not tasks:
        return []

    if _uses_github_issues(project_context):
        return _materialize_to_github(tasks, project_context, store, client)

    path = _resolve_fallback_path(project_context)
    if path is not None:
        _materialize_to_fallback(tasks, path, store)
    return []
