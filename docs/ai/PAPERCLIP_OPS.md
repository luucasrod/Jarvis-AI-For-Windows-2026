# Paperclip operations (#18)

Use `orchestrator.paperclip_ops.create_task_idempotent(company_id, title,
description, correlation_id, assignee_agent_id=None, store=store)` and
`get_task_status(company_id, task_id)`. Both return dictionaries with
`available`; failures include `reason`. Successful creation includes `task_id`,
`task`, `cached` and `reconciled`; status reads include `status` and `task`.

The wrapper uses the existing `paperclip_client.create_task` and its HTTP helpers.
No parallel HTTP implementation or changes inside Paperclip are introduced.
New calls consume the orchestrator base URL and timeout configuration; existing
voice calls retain their default URL/timeout. `PAPERCLIP_API_TOKEN` from the
environment takes precedence over the legacy local config token. Errors omit
arbitrary exception text and server bodies that could echo credentials.

Idempotency is scoped to server URL, company and correlation. Different content
with the same local operation identity returns `correlation_conflict`. The
`paperclip_creations` table stores an atomic unique claim and the remote result;
the existing `idempotency_keys` table records confirmed creation. A crash between
result persistence and key persistence is repaired from the saved result.

Before creating, the wrapper searches for a deterministic marker in the remote
description. The marker is appended without changing the requested task content.
Only the winner of the local claim can POST, including across Store connections.
If a POST times out or returns an unusable result, the claim remains uncertain.
A later call searches the marker and recovers the existing task when visible.
It never issues another POST merely because that search returned no match.

This is intentionally conservative: a process that dies after claiming but
before sending also leaves an uncertain attempt. `creation_unconfirmed` needs
reconciliation by the caller/operator; there is no automatic unsafe claim reset.
Read failure before any claim is safe to retry. Local data loss, independent
databases issuing the same operation, or a remote marker removed by another
actor are outside the local exactly-once guarantee. No retry thread is started.

Status reads and reconciliation use the company issue collection. Paging is
bounded to 50 pages of 100 items; malformed/repeated/truncated reads return an
error instead of proving absence. Status lookup matches the exact task ID.
The API contract is documented in the [Paperclip Issues API](https://docs.paperclip.ing/reference/api/issues/)
and the upstream [issue service](https://github.com/paperclipai/paperclip/blob/master/server/src/services/issues.ts).

Tests mock HTTP for creation, restart, concurrent claims, lost replies, local
write failures, malformed responses, status, offline and timeout. A read-only
smoke against the local server also returned an existing task's status. No real
task was created or executed as part of validation.
