# GitHub issue client (#17)

Create `GitHubClient(store)` and call
`create_issue(repo, title, body, labels, correlation_id)`. Success returns
`available`, `number`, `url`, `operation_key` and `cached`. Failure returns
`reason`, with `pending`, `uncertain` and `next_attempt` when an operation exists.
Repository names use owner/name on github.com; gh supplies existing authentication.
No token is embedded in commands, results or logs.

`list_issues(repo, state='open', labels=None)` returns `available` and `issues`,
or a structured error. It uses gh api pagination, validates returned pages and
excludes pull requests from the issue collection. State may be open/closed/all;
labels follow GitHub's comma-separated filter semantics. JSON stdin and argument
arrays preserve newlines and literal shell characters without shell evaluation.

`retry_pending(limit=20)` processes due operations once and returns their results.
The runtime schedules those calls; the client never starts threads or sleeps.
RETRY_INTERVAL_SECONDS is the initial delay, doubled after each attempt up to
max_backoff_seconds (default 3600). Clock, subprocess runner, timeout (default
30 seconds) and maximum backoff are injectable. A supplied clock must be aware.

## Persistence and retries

Intent is persisted in pending_github_ops before any network access. Identity is
scoped to normalized repository and correlation; changed content with the same
identity is rejected. Confirmed number, idempotency_keys and task_created event
commit atomically. A cached result requires no remote access. Reusing an old issue
by title does not emit a new creation event.

Before POST, the client searches all issue states for its description marker,
then an exact trimmed/case-insensitive title match. Multiple matches are reported
as ambiguous. Label changes cannot hide a duplicate from this preflight.

Atomic claims serialize each operation across SQLite connections. An expired
read lease can be taken over; the old owner must still prove ownership before
POST. The database records uncertainty before sending, so a crash during POST
can only trigger reconciliation, never a blind repeat.

Read failures, missing gh and clearly unsent connection failures are retried with
backoff. Definite HTTP validation failures (400/404/422) remain persisted as
failed and need input/operator correction. Authentication/rate-limit failures
back off. stderr and server response bodies are never surfaced as error text.

A lost POST reply is recovered when its marker appears remotely. Without that
match, creation_unconfirmed stays pending and only reconciliation is retried.
This conservative case includes a crash between recording posting and actually
sending. There is no unsafe automatic reset. Separate databases or loss of local
state are outside the local concurrency guarantee; GitHub does not provide the
client with a transactional commit shared with SQLite.

The additive Store.run_in_transaction and events.emit_in_transaction helpers are
identical to those in scheduler PR #82; after integrating both PRs retain one
definition of each. They commit only local SQLite effects; no transaction stays
open during a gh command. Existing Store methods keep their behavior.

The command and API contracts follow [gh api](https://cli.github.com/manual/gh_api)
and [GitHub's issue endpoints](https://docs.github.com/en/rest/issues/issues).
Tests use an injected gh runner, simulate restart/backoff/concurrency/lost replies
and force a transaction rollback. Real smoke issue #88 was created once, returned
again from cache, verified with one event, and then closed.
