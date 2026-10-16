# Developer MCP

A local Python MCP service with independent skill and task features. Agents can
borrow complete skill packages, manage workspace-scoped tasks and reminders, and
save raw “where I left off” notes. Optional explicit delegation uses a durable
queue and a tested worker contract. Production agent runners, AI normalization,
and proactive notifications are **not included**.

Personal and work connections use different credentials and private storage.
The original `/mcp` endpoint stays personal and skills-only, with its existing
library, IDs, copies, and token. No personal skills or tasks are migrated to work.

## Start the shared service

Requires Linux, Python 3.12+, and `uv`. From this checkout:

```sh
uv sync --locked --all-groups
uv run dev-mcp service install
uv run dev-mcp service status
```

Installation writes a **user** systemd unit, enables it at user login, and starts
it. It restarts on failure. No system-wide service or user lingering is enabled.
The service uses this environment's Python, so keep the checkout and `.venv` in
place; reinstall the unit if you move them. On systems without a working user
systemd manager, run the foreground server:

```sh
uv run dev-mcp serve
# Or select a port:
uv run dev-mcp serve --port 9876
```

The default endpoint is `http://127.0.0.1:8765/mcp`. One daemon can run per state
location. Commands never launch fallback servers for a disconnected client.

```sh
uv run dev-mcp service stop
uv run dev-mcp service start
# Persist a different port and reinstall the unit:
uv run dev-mcp service install --port 9876
```

Only loopback binding is supported. Remote/cloud delivery and agent-specific
native installation are deferred.

## Connect a client

Use the client's **Streamable HTTP MCP** connection settings:

| Setting | Value |
| --- | --- |
| URL | `http://127.0.0.1:8765/mcp` (or configured port) |
| Authorization header | `Bearer <contents of the private token file>` |
| Token file | `$XDG_CONFIG_HOME/dev-mcp/token`, default `~/.config/dev-mcp/token` |
| Filesystem access | Read/write access to managed acquisitions in the state directory |

Use a client's secret/header facility to supply the token. Do not commit it in
client configuration. The token is generated on first HTTP startup or service
installation, stored with mode `0600`, and never returned by tools. This is a
local pre-shared token, **not an interactive OAuth login service**. On this legacy endpoint, clients
with the token act as the same personal skills owner; recorded client/task labels are
assertions, not separately authenticated identities. Host and Origin validation
is enforced by the MCP SDK. A browser Origin must match the configured local
HTTP origin. There is no public network listener.

A client must be able to read and edit the returned local paths. If its sandbox
cannot access them, grant access through that client's normal controls. The server
does not copy skills into a consuming checkout as a workaround. A remote client
cannot use these paths without a separate filesystem/delivery solution.

Compatibility stdio remains available:

```json
{
  "command": "/absolute/path/to/dev-mcp/.venv/bin/dev-mcp",
  "args": ["--profile", "legacy", "stdio"]
}
```

`--profile NAME` is required for stdio; use `legacy` for the original skills
connection. Omitting the profile fails without starting a fallback server. Each
stdio connection creates a full separate process; direct HTTP connections share one process and use less memory
with multiple clients. Stdio uses local process access instead of bearer auth.
Stdout carries only protocol messages; diagnostics go to stderr.

## Configure workspace access

Owner CLI commands run locally. Create separate credentials for each workspace
and, when useful, each client. The default new profile has task, reminder, and
pause read/write permissions; it has no skills, delegation, or worker access.

```sh
uv run dev-mcp workspace list
uv run dev-mcp profile create personal-agent --workspace personal
uv run dev-mcp profile create work-agent --workspace work
# Optional narrow read-only connection:
uv run dev-mcp profile create work-reader --workspace work --permissions tasks.read
uv run dev-mcp service stop
uv run dev-mcp service start
```

Use `/mcp/personal-agent` or `/mcp/work-agent` on the same host/port. Each command
prints the endpoint and private credential **file path**, never the token. Tokens
live in `~/.config/dev-mcp/credentials/<profile>.token` (mode `0600`); provide the
matching file's contents through the client's secret/header facility. The original
`/mcp` still uses `~/.config/dev-mcp/token`. Credentials cannot be used at another
profile's endpoint. New endpoints require a service restart; rotation, revocation,
and workspace delegation-policy changes take effect on subsequent requests.

```sh
uv run dev-mcp profile rotate work-agent
uv run dev-mcp profile revoke work-reader
```

Permissions are `tasks.read`, `tasks.write`, `reminders.read`, `reminders.write`,
`pauses.read`, `pauses.write`, `skills`, `delegate`, and `worker`. Write requires
the corresponding read permission; `delegate` also requires task read/write.
A worker profile must contain only `worker`. Disabled tool groups are omitted from
the profile's tool catalogue, and every operation checks its scope. Up to 32
workspaces and 64 profiles are supported. `workspace_context` reports the fixed
identity and enabled capabilities. A tool cannot switch workspace/profile.

Stdio selects the same binding using `--profile work-agent stdio`. Local process
access replaces HTTP bearer authentication. Revoked or changed profiles cannot
continue operations through an already-open stdio connection. Profile credentials
identify a capability, not an individual agent; agents sharing a profile share its
ownership authority. Use distinct profiles when that distinction matters.

This is an **MCP access boundary**. Unrestricted filesystem/process access under
the same OS account can read state, credentials, or select another stdio profile.
Execution sandboxing is deferred to production runner delivery. Give agents only
the filesystem and credential access they need.

Personal skill paths remain unchanged. For another workspace, enabling `skills`
uses its empty private `workspaces/<workspace>/library/` and `skills-state/` under
XDG state. Add skills there explicitly; nothing is shared or auto-migrated.

## Tasks, reminders, and pause notes

| Tools | Purpose |
| --- | --- |
| `workspace_context` | Current scope, permissions, timezone, and available features |
| `tasks_create`, `tasks_list`, `tasks_get` | Save/search tasks; retrieve details, context, history, attempts, delegations |
| `tasks_update` | Patch title, instructions, priority, due, checkout, context using an expected version |
| `tasks_transition` | `complete`, `cancel`, `block`, or `reopen` an unclaimed task |
| `tasks_archive` | Archive or restore, independently of task status |
| `tasks_claim`, `tasks_report` | Record an existing agent's ownership and progress; launch nothing |
| `reminders_create`, `reminders_list`, `reminders_update` | Queryable dates; reschedule, acknowledge, cancel |
| `pauses_save`, `pauses_find`, `pauses_update` | Immutable raw notes, recent/context lookup, resumed/archive metadata |
| `tasks_delegate`, `delegations_cancel` | Explicit frozen execution queue; requires separate permission and configuration |
| `worker_claim`, `worker_heartbeat`, `worker_finish` | Restricted worker contract, tested with a fake worker |

Task states are `open`, `in_progress`, `blocked`, `done`, and `cancelled`.
Priority ranges from 0 to 9, highest first. Repository/worktree paths and provider
choices are optional for generic tasks. There is no permanent deletion tool.
Archiving hides a task from default lists and is reversible.

Every task-feature mutation requires a caller-generated `request_id` (UUID
recommended). Retries with the same profile, operation, ID, and arguments return
the original result, even if the record has since changed. Reusing an ID with
changed arguments fails. Edits, lifecycle changes, and terminal reports require
`expected_version`; retrieve current state before a fresh attempt after conflict.
Successful mutations and their retry receipts commit together. Failed task
mutations roll back and do not create a success receipt. Skill retry behavior
remains as documented below; non-legacy skill request IDs are also profile-scoped.

An existing agent calls `tasks_claim` with its session reference, then works using
its own capabilities. Claiming records an attempt and sets `in_progress`; it
launches nothing. Use `tasks_report` with `heartbeat`, `progress`, `complete`,
`block`, `release`, `cancel`, or `fail`. Heartbeat within 300 seconds and use the
returned version for the next report. Results support a bounded `summary` and
`references` (including provider/session or output references, never secrets).
Simple unclaimed to-dos can be completed directly. Generic edits cannot bypass
an active claim or queued delegation. Overlapping writable checkout paths are
reserved across all workspaces; use separate worktrees for concurrent writing.
An unavailable checkout reservation is reported without exposing another
workspace's task or owner.

Reminders belong to tasks, with multiple reminders allowed. Supply an ISO timestamp
with an explicit offset and optionally an IANA `timezone` (default Europe/Berlin).
Local offsets must match the named zone; UTC `Z`/`+00:00` identifies an absolute
instant. Naive timestamps and nonexistent local times are rejected; an offset
selects which occurrence of a repeated daylight-saving hour you mean. Saved results
include UTC time and the chosen zone. Past times require `allow_past=true`.
The calling agent resolves phrases such as “next month”; the server never guesses.
Task `due` accepts a calendar date or offset timestamp, independently of a reminder
or delegation's `not_before`.

`reminders_list` accepts `upcoming`, `due`, `overdue`, or `all`, optional time
bounds and task/status filters. “Due” and “overdue” both mean pending times at or
before the query instant; no grace period or notification scheduler is implied.
Reading does not acknowledge. Acknowledgement does not complete the task.
Completing, cancelling, or archiving a task cancels pending reminders atomically;
restoring/reopening never reactivates them automatically.

`pauses_save` preserves `raw_text` exactly (including whitespace), with optional
task, canonical checkout/worktree, and originating `session_ref`. It immediately
returns a durable ID. Notes need neither a task nor a repository. Use `pauses_find`
for recent unresolved notes, filtered by context or text, or fetch an exact ID.
Matches stay separate and reading does not resume them. `pauses_update` changes
resumed/archive flags or contextual metadata. Correct raw text by saving a new
checkpoint with `supersedes=<old-id>`; the original remains retrievable. These
operations never change task state, control an agent, or enqueue work. They never
inspect API keys, invoke a model, or launch a normalization subprocess.

## Context and delegation

Task `context` contains up to 32 entries with `kind`, `value`, and optional `label`:

- `snapshot`: inline text fixed with the task/attempt snapshot.
- `managed`: immutable file snapshot ID imported by the owner CLI.
- `file`, `url`, `skill`, `retrieval`: references deliberately resolved by a future
  executor at execution time. The MCP server does not fetch or execute them.
- `credential_ref`: a credential reference/name, never its secret value.

Inline instructions and inline context each have an 8 KiB serialized limit. Put
large context on disk. To capture a local file without letting a tool read
arbitrary paths on the server:

```sh
uv run dev-mcp workspace context-import personal --path /absolute/path/brief.pdf
```

Use the returned ID in `{"kind":"managed","value":"<id>"}`. `tasks_get` with
`view="context"` and `context_index` returns metadata and a local path, plus up to
2,048 bytes of UTF-8 preview when decodable. `offset` is a byte offset for this
view. Binary data is never returned as base64. Cross-workspace context IDs fail;
local paths require filesystem access. Imports stream in 1 MiB chunks and do not
hold the task lock while copying. Defaults are 256 MiB per context file, 4,096
imports and 2 GiB per workspace context store, separately from skill storage.
Changed sources and unfinished imports are preserved and reported, never silently
adopted as a complete snapshot. No automatic pruning is performed.

Delegation is disabled by default. To exercise the queue contract explicitly:

```sh
uv run dev-mcp workspace configure personal --delegation-enabled --execution-profile fake
uv run dev-mcp profile create delegator --workspace personal --permissions tasks.read,tasks.write,delegate
uv run dev-mcp profile create fake-worker --workspace personal --permissions worker
```

`workspace configure` replaces the workspace timezone/delegation configuration;
its defaults are Europe/Berlin and delegation disabled. It never starts a worker.
Restart the service to expose newly created profiles. A delegator may enqueue
only configured execution-profile names. A queued request freezes the task version,
instructions, snapshot IDs, and context references; no eligible worker means
`waiting_for_worker`. Editing a queued task requires cancelling that delegation
first. Creating tasks, reminders, and pause notes never delegates them.

A worker explicitly polls `worker_claim` using a fresh request ID per poll, its
supported execution profiles and durable session reference. Selection is highest
priority then creation time, with `not_before` respected. Each poll examines at
most 50 eligible requests and skips occupied checkouts; unresolved capacity is
reported. The deployment permits one active/uncertain worker attempt globally.
The attempt is committed before being issued, including frozen instructions.
The worker must preserve its attempt ID before any future provider launch, send
`worker_heartbeat`, and use `worker_finish` with the latest version for its terminal
outcome. An optional `context_index` on a heartbeat retrieves bounded managed
context for that worker's own frozen attempt; it grants no general task-reading
access. Reuse a request ID only to retry the exact call; replayed empty polls stay
empty. A future polling loop should use bounded configurable intervals (for example
5–60 seconds with backoff); this release starts no polling loop or worker service.

Cancelling active work records `cancel_requested`; the worker must confirm stopping
with `worker_finish(action="cancel")`. A heartbeat communicates cancellation and
its new version. Expired leases become `uncertain` when task storage is next
queried; reservations remain held. There is no automatic replacement on disconnect,
restart or timeout. See [recovery](docs/recovery.md) for explicit owner reconciliation.
Production Codex/Claude adapters and their sandbox/approval integration remain deferred.

## Add skills manually

Create `skills/<skill-id>/SKILL.md` and supporting files. The directory is the
stable ID, and `name` must match it. Example (not installed automatically):

```text
skills/my-review/
  SKILL.md
  references/checklist.md
  scripts/check.py
  assets/example.png
```

```markdown
---
name: my-review
description: Review a change using my personal checklist.
compatibility: Python 3.12 and Git when running the optional check script.
metadata:
  author: Ammar
---
Read references/checklist.md, then review the requested change.
Write task outputs outside this skill directory.
```

Use the [Agent Skills format](https://agentskills.io/specification). IDs contain
1–64 lowercase letters/digits separated by single hyphens; descriptions contain
1–1,024 characters. Optional `compatibility` is at most 500 characters; `metadata`
maps text keys to text values. `SKILL.md` is UTF-8, at most 1 MiB, with at most
16 KiB of YAML frontmatter. Duplicate YAML keys and aliases are rejected. Packages
may contain nested text/binary assets and executable scripts; links and special
files are rejected. Executable flags and empty directories are preserved.
The server never executes scripts or installs declared prerequisites.

The configured library path is independent of client checkouts. For a wheel
installation, supply it explicitly:

```sh
dev-mcp --library /absolute/path/to/dev-mcp/skills service install
```

## Tools and workflow

All tools return structured data and a text equivalent, with bounded responses.
The following eight tools are exposed only to profiles with `skills` permission.

| Tool | Inputs and purpose |
| --- | --- |
| `skills_search` | `query=""`, `offset=0`, `limit=20`; list/search summaries, max page 50 |
| `skills_inspect` | `skill_id`; instructions, declared prerequisites, revision, size and manifest path |
| `skills_session_open` | `request_id`, `client`, absolute existing `checkout`, `task`, optional `task_ref` |
| `skills_acquire` | `request_id`, `session_id`, `skill_id`, optional expected `revision` |
| `skills_sessions` | Optional `session_id`, `checkout`, `client`, `task`; outstanding by default, paginated |
| `skills_release` | Exactly one of `acquisition_id` or `session_id`; optional `close_session` |
| `skills_update_prepare` | `request_id`, `acquisition_id`, explicit `changes`, `human_instruction`, `reason` |
| `skills_update_apply` | `request_id`, `proposal_id`; publish the frozen proposal and return an audit receipt |

1. Search and inspect the requested skill.
2. Open a session for the client, canonical checkout/worktree and task.
3. Acquire a package, then explicitly read its returned `skill_path`. Each
   acquisition has its own writable directory. No files are placed in the checkout.
4. Keep outputs elsewhere. The ledger records **copy issued**, not that the agent
   read or executed it, or installed it in an agent's skill picker.
5. Release the acquisition or session. Set `close_session=true` to close after
   cleanup succeeds. Review any unresolved paths; they remain intact.

Creation and update request IDs are generated by the caller (UUIDs work well).
Use the same ID and arguments to retry an operation; different arguments with an
old ID are rejected. A retry returns its original result, even after later cleanup;
use `skills_sessions` for current state. Recorded failures are also replayed.
Interrupted requests report their durable object ID instead of repeating work.

For a lost session ID, filter `skills_sessions` by checkout, client, or exact task
label. With `session_id`, paginate acquisitions using `next_acquisition_offset`
and proposals using `next_proposal_offset`; all offsets use the `offset` input.
Session-wide release processes every copy but includes at most 20 summaries;
per-copy cleanup reports and subsequent session lookup provide the rest.

## Explicitly requested source updates

Only prepare updates when the human explicitly asked to change the original
skill in the current workspace. Quote that instruction and explain the reason. Example `changes`:

```json
[
  {"path": "SKILL.md", "action": "replace"},
  {"path": "assets/example.png", "action": "replace"},
  {"path": "references/new-note.md", "action": "add"},
  {"path": "scripts/obsolete.py", "action": "delete"}
]
```

Preparation freezes a complete candidate package, incorporating only these paths.
Other generated files never enter the library. It produces a local full preview
and a bounded inline preview; text diffs are limited to 64 KiB per input file and
2,000 combined lines, with hashes/sizes for binary or larger files. Frozen files
remain available for inspection until successful application or rollback.

The human quote is an **agent assertion of authorization**, not independent proof.
The server records it, the reason, session/client, selected paths and revisions.
There is no second human approval step. A profile with `skills` permission grants this capability;
only connect agents you trust to follow your instructions.

Apply the proposal ID with a fresh request ID. Both preparation and application
check the original revision. Conflicts never merge or overwrite newer work.
Changing the borrowed copy after preparation cannot change the proposal. Skill
renaming, removing `SKILL.md`, invalid metadata and unsafe paths are rejected.
Published changes remain in the library's working tree: nothing is staged,
committed or pushed automatically.

## State, cleanup and recovery

Defaults (XDG environment variables are respected):

| Location | Contents |
| --- | --- |
| `~/.config/dev-mcp/` | `settings.json`, private legacy `token`, `access.json`, `credentials/` |
| `~/.local/state/dev-mcp/workspaces/<workspace>/` | Private `tasks.sqlite3`, `context/`; non-personal `library/` and `skills-state/` |
| `~/.local/state/dev-mcp/state.sqlite3` | Sessions, acquisition manifests, proposals, request results, audits |
| `~/.local/state/dev-mcp/acquisitions/` | Individually owned borrowed directories |
| `~/.local/state/dev-mcp/proposals/` | Frozen candidates and previews |
| `~/.local/state/dev-mcp/transactions/` | Pending publication journals and backups |
| `~/.local/state/dev-mcp/manifests/` | Local manifests and cleanup/large-response reports |

Managed directories are private (`0700`). Runtime locations inside Git checkouts
are rejected. Overrides: `DEV_MCP_CONFIG`, `DEV_MCP_STATE`, `DEV_MCP_LIBRARY`, or
`--config`, `--state`, `--library` before the subcommand. Service installation
persists absolute paths. Do not put runtime state or credentials in version control.

Release checks hashes, executable flags and file/directory identities. It only
removes matching owned files and known empty directories. Edited, replaced,
unexpected and linked paths survive; unresolved sessions stay open. Exact
published edits can become the new cleanup baseline; later/unpublished changes
stay protected. Directory listings for unexpected outputs are bounded; a directory
reported as unresolved may contain more files than the report lists.

Disconnects, elapsed time and restarts never delete issued copies. Unissued
capture staging is disposable. Pending proposals survive, including when their
borrowed copy was released; session lookup also finds sessions with pending
proposals. Do not routinely delete the state directory to free space.

Startup checks transaction journals only, not every skill package. An unfinished
publication rolls back only files that still match the recorded original or the
server's written content and identity. A conflicting external edit is preserved
and reported by `skills_sessions`; further writes to that skill are blocked.
See [recovery procedures](docs/recovery.md) before touching journals or backups.

## Resource limits and validation

Defaults in `settings.json` (restart all processes after changing shared limits):

| Setting | Default |
| --- | --- |
| `package_bytes` | 256 MiB per package |
| `package_files` | 4,096 files; also at most 8,192 total filesystem entries |
| `storage_bytes` | 2 GiB of managed package/staging/backup storage |
| `file_workers` | 2 file-operation threads/shared admission workers |
| `waiting_operations` | 16 additional operations across shared-state processes |

Task and skill operations have separate shared admission pools (two active and
sixteen waiting each), shared across profiles and compatibility processes. Task
transactions use a separate short lock and do not wait for skill copies.
Requests receive explicit capacity errors; active work is never evicted. Durable
filesystem mutations are serialized across HTTP and stdio processes with a local
file lock. Two workers bound admitted filesystem work; this version prioritizes
consistent publication/cleanup over parallel writes. Copies and hashes stream in
1 MiB chunks. SQLite stores metadata, not binary contents. No folder watchers,
polling catalogue refreshes, database frameworks or package-byte caches are used.
Catalogue metadata refreshes on search. Tool arguments are limited to 64 KiB,
HTTP request bodies to 128 KiB, and tool responses to 64 KiB including duplicated
text/structured content. Large skill results have a local file; task/history pages shorten to fit and
return `next_offset`; combined records must also fit the serialized result budget.
Raw pause text has a 16 KiB serialized limit. `latest-*` skill reports are
replaced by the next oversized response from that tool.

[Memory measurements](docs/memory.md) include two HTTP clients, large packages,
repeated sessions and two compatibility processes, workspace task cycles and large durable histories.
Measurements assess growth
and allocations; there is no arbitrary fixed memory cap.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python scripts/measure_memory.py
```

Tests cover the skills and task/worker tools in process, over real stdio, and over shared HTTP,
including auth/origin rejection, concurrent clients, unchanged consuming checkouts,
package safety/limits, retries, cleanup, frozen binary updates and crash recovery.
Tests and the memory benchmark use temporary libraries and state, not personal skills.
