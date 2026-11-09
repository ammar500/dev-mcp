# Recovery and retention

The service keeps durable task sessions and issued copies. A crashed or
disconnected client does not authorize deleting its work. Find lost sessions with
`skills_sessions` using checkout, client or task, or inspect everything locally:

```sh
uv run dev-mcp recovery status
```

This also exposes pending proposal IDs and source-recovery conflicts. Inspect the
referenced local manifests, previews and journals. Retried creation/update calls
return the first result; `interrupted_request` means execution ended without a
final result and includes the recorded object ID. Consult the ledger before
starting the operation again with a new request ID.

## Unresolved borrowed files

`skills_release` deletes unchanged owned files and preserves edited, unexpected,
replaced and linked paths. It records per-copy reports under `manifests/` and
keeps the session open when anything remains.

For each reported path, decide whether to publish the human-requested edit, move
it to a safe output directory, or deliberately remove it yourself. If the
acquisition is unresolved, its remaining selected edits can still be prepared
and published against a current original revision. Then repeat release. Never
recursively delete a borrowed directory just because the session seems old.

If you replaced an acquisition directory itself, the server cannot establish
ownership of the replacement and will preserve the entire directory. Resolve it
manually and repeat release; missing owned files are harmless.

## Interrupted publication

Original-file changes use a cross-process lock and a durable transaction journal.
Before modifying originals, the service records backups and each path's before
and after hashes, executable flags, and file identities. Replacement files are
written and synced next to their destinations; their identities are journaled
before atomic rename. The final database transaction records the audit receipt
and idempotent result before disposable backups are removed.

On startup:

- Committed updates keep their new source content; their disposable staging is removed.
- Uncommitted updates roll back only files identifiable as the transaction's work.
- Later external edits and replaced files are preserved and reported as conflicts.
- Conflicted skills are blocked for acquisition/preparation/publication until recovery.
- Issued acquisitions remain untouched. Only never-issued partial capture staging
  and source backups made before journal publication are disposable.

If there is a conflict, stop the background service and other clients before
manual repair:

```sh
uv run dev-mcp service stop
```

Read `transactions/<proposal-id>/journal.json`, its `backup/` files and the frozen
`proposals/<proposal-id>/package/`. Save any versions you need outside managed
storage. Reconcile the original skill directory manually to the desired valid
package. Do not edit SQLite or falsify journal hashes/inodes to force a rollback.

Once the current source is deliberately the version you want, use this explicit
local owner command:

```sh
uv run dev-mcp recovery resolve <proposal-id> --keep-current-source
uv run dev-mcp service start
```

This validates and records the current source revision without changing it,
clears the conflict, and discards **that transaction's** backup and frozen proposal.
It is an explicit recovery action, not an additional approval step for ordinary
updates. Borrow a fresh copy after reconciling source revisions. If validation
fails, the backups and conflict remain for further repair.

## Pending proposals and storage pressure

Prepared proposals freeze selected edits even if their acquisition later changes.
They are not removed on timeout, disconnection, or session closure. A released
acquisition's pending proposal remains discoverable; its frozen files may be the
only copy of unfinished work. To resume after releasing the acquisition, preserve
those edits, borrow the current original again, and prepare against that revision.

Successful publication/rollback removes disposable package staging and backups,
while SQLite retains manifests, request results and compact audit events. Failed
preparations discard unissued staging; the borrowed edits remain. Capacity errors
never evict active acquisitions or pending proposals. Release finished work first.
If manually retiring an abandoned proposal, stop all server processes, preserve
needed content first, and retain the database/audit history. A missing pending
proposal will fail safely on application; it will not silently be regenerated.

All processes sharing a state directory must use the same library and limits.
The database and local filesystem form one recovery unit; back them up together
with the originals when backing up this service. Restoring only one component
can produce revision or file-identity conflicts, which are intentionally preserved
for inspection.

## Task ownership and cancellation

Task data is separate from skill sessions. Each workspace has its own
`workspaces/<workspace>/tasks.sqlite3` with WAL, indexed records, and schema
versioning. Task queries and retry receipts do not load complete histories into
memory. Scope is fixed by the profile; there is no cross-workspace lookup tool.

Find tasks with `tasks_list`, then use `tasks_get` with `view="attempts"`,
`"delegations"`, or `"history"`. Pages use `next_offset`. Reminders and checkpoints
have their own list/find tools; an archived/cancelled task does not erase its
checkpoints. Reading reminders or pause notes does not acknowledge/resume them.

A successful mutation and its retry result commit atomically. Retry the exact
request ID and arguments after a lost response. An original successful result
may be stale; fetch current state before proceeding. Failed mutations roll back.
After a version conflict, fetch the record and submit the corrected action with
a **new** request ID.

External agents and workers have 300-second heartbeat leases. On a subsequent
task operation, expired attempts become `uncertain`, with an audit event. Their
checkout reservations and worker slot remain occupied. Starting/restarting MCP
does not launch, kill, or replace an agent. An HTTP disconnect alone cannot prove
that an external agent stopped. A cancellation request also cannot prove stopping:
`cancel_requested` is retained until a worker reports cancellation or owner
reconciliation resolves uncertainty.

Before releasing an uncertain reservation, inspect the recorded session/provider
references and the actual process/worktree. Stop any surviving writer and preserve
its uncommitted work. Then the local owner can explicitly attest it is stopped:

```sh
uv run dev-mcp workspace reconcile personal --attempt-id <id> --confirmed-stopped
```

This records reconciliation, releases ownership, and sets the task to `blocked`.
It does not kill a process, touch the checkout, or automatically requeue. Review
current instructions and deliberately claim/delegate again if appropriate. If a
profile was revoked, its outstanding attempts still need reconciliation; recreating
a profile is not a safe substitute for inspecting the old session.

## Testing queue recovery

Testing tickets have **no lease or automatic expiry**. Waiting agents poll; active
holders do not. Silence, disconnect, age, and server restarts never release either.
Use `testing_list` through a profile with `testing.read` in the relevant workspace
and filter by repository, worktree, session, or ticket ID. Default results include
waiting/active/recovery-required records; use `status="all"` for completed records.
A revoked profile's tickets remain discoverable through another authorized reader.

The owner can explicitly retire an abandoned **waiting** ticket:

```sh
uv run dev-mcp workspace testing-recover work --ticket-id <id> --reason "Session abandoned; withdrawn explicitly"
```

For an active or restored holder, inspect the actual stack and main checkout first.
Stop the stack, preserve needed work, and finish cleanup. Then confirm both facts:

```sh
uv run dev-mcp workspace testing-recover work --ticket-id <id> --reason "Stopped the abandoned stack and checked cleanup" --stack-stopped --cleanup-complete
```

This records the local owner's UID, previous state, reason, and confirmations;
it marks the ticket `reconciled`. It never kills a process, modifies Git, or grants
the next ticket automatically. Repeated recovery of a terminal ticket is harmless.
Do not use recovery just because an agent has been quiet. A queue blocked by an
unavailable checkout or missing worktree stays blocked until explicitly resolved.

Normal agents call `testing_done` to withdraw or finish their own tickets. An active
holder must confirm stack shutdown and cleanup; an unsuccessful cleanup leaves its
reservation in place. Restored `recovery_required` tickets require the owner CLI,
not `testing_done`. Task-attempt reconciliation and testing recovery are independent:
releasing one never implicitly releases the other.

Before restoring a workspace snapshot, stop all testing agents and all clients
across the deployment, including those in other workspaces sharing a repository.
Old ticket grants must not be reused after restore. Restored holders stay blocking
even if another workspace has newer records. Inspect and reconcile the real stack
before allowing any waiter to proceed. Preserve all existing data first; restore
never overwrites a present workspace.

## Workspace task backups and restore

Stop the shared service, every stdio server, any external task writers and workers
before restoring. For a complete deployment snapshot, stop them before backup too.
`workspace backup` rejects a running shared daemon and serializes the SQLite/context
snapshot with task operations. Owner CLI operations are serialized with each other.
Never copy only a live SQLite main file while ignoring its WAL.

```sh
uv run dev-mcp service stop
uv run dev-mcp workspace backup personal --path /private/backups/personal-2026-10-16
```

The destination must be new and outside Git. The command uses SQLite's backup API
and streams managed context with checksums into a private directory. It includes
tasks, reminders, checkpoints, delegation/attempt records, testing tickets, retry
receipts and audit history. Inspect `backup.json` for the source workspace and context manifest.
A failed backup is incomplete and preserved for inspection; retry with a new path.

**This command backs up workspace task and testing data.** It excludes profile credentials,
configuration, skills/originals/borrowed copies, and external context references.
For a full deployment backup, also preserve `settings.json`, `access.json`, private
credentials, the legacy personal skill state/library, and each non-personal
`library/` plus `skills-state/`, while all servers/writers are stopped. Keep secrets
in private backup storage. Skill file identities may change on restore; follow the
skill recovery rules above rather than deleting unresolved copies.

Restore is same-workspace only and refuses an existing workspace directory. Move
its entire existing directory to a private preservation location first; do not
delete newer or unfinished work to make room. Recreate owner configuration if
needed, preserving workspace and profile IDs so retry/ownership records retain
their meaning. An older backup never supersedes a still-running newer attempt.

```sh
uv run dev-mcp workspace restore personal --path /private/backups/personal-2026-10-16 --confirmed-stopped
```

Restore stages a database, checks integrity/schema and context checksums, renews
managed-context file identities, then publishes the directory atomically. Active
attempts and active delegations become `uncertain`. Active testing tickets become
`recovery_required` and retain their slots/checkout reservations. Waiting tickets
keep their order and remain blocking. Reconcile holders before retrying.
If startup finds a leftover `<workspace>.restore` directory after a hard crash,
preserve/inspect it and retry only after moving that unpublished staging aside.
The published directory is never overwritten by restore.

For a non-personal workspace with skills enabled, task restore does not reconstruct
its skill library/state. Restore those preserved components to their original
locations before starting clients. The legacy personal library and skill state
remain at their existing paths, outside the task workspace directory.

## Schema migration, context imports, and retention

The task database is new and independent of the existing skill database. Startup
creates schema version 2 for empty databases and atomically upgrades version 1
with the testing-ticket table and indexes. Existing records and credentials remain
unchanged; old version-1 backups are migrated in private restore staging. Unknown
schema versions fail closed; there is no destructive downgrade. Take a consistent
backup before future migrations. Config schema version 1 similarly rejects unknown
versions. Migration never moves existing skills or enables task access on `/mcp`.
New task profiles must be explicitly created through the owner CLI.

Managed context imports reserve capacity before copying, copy without holding the
task transaction lock, and publish their metadata only after a stable capture and
file sync. Interrupted/changed captures remain `pending` and cannot be attached to
tasks. Their files and quota reservation stay intact. Inspect them in the workspace
context directory and database while servers are stopped; export useful content,
then import the intended stable source as a **new** snapshot. There is no automatic
pruning or general delete/import-repair tool in this release. Raise context limits
or preserve/export a retired workspace if capacity is reached; do not falsify ready
states, checksums or file identities. Replaced/edited ready context fails retrieval.

Task histories, requests, raw pause notes and unfinished queue records are retained
on disk without an in-process history cache. No elapsed-time cleanup or silent
eviction is performed. Context imports have bounded file/count/byte quotas; SQLite
metadata history can grow on disk. Monitor available disk space and preserve
backups. Storage failures roll back task mutations and return actionable errors.
Production agent execution, normalization, proactive reminders and process-level
sandbox recovery remain outside this release.
