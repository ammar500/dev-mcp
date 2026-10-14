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
