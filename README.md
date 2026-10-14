# Developer MCP

A local Python MCP service for development tools. Skill management is the first
feature: agents can search your library, borrow complete packages outside their
checkouts, publish explicitly requested edits, and release their copies safely.
The library starts empty; existing personal skills are never imported automatically.

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
local pre-shared token, **not an interactive OAuth login service**. All clients
with the token act as the same local owner; recorded client/task labels are
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
  "args": ["stdio"]
}
```

`dev-mcp` without arguments also runs stdio. Each stdio connection creates a full
separate process; direct HTTP connections share one process and use less memory
with multiple clients. Stdio uses local process access instead of bearer auth.
Stdout carries only protocol messages; diagnostics go to stderr.

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
personal skill. Quote that instruction and explain the reason. Example `changes`:

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
There is no second human approval step. The bearer token grants this capability;
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
| `~/.config/dev-mcp/` | `settings.json`, private `token` |
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

Requests receive explicit capacity errors; active work is never evicted. Durable
filesystem mutations are serialized across HTTP and stdio processes with a local
file lock. Two workers bound admitted filesystem work; this version prioritizes
consistent publication/cleanup over parallel writes. Copies and hashes stream in
1 MiB chunks. SQLite stores metadata, not binary contents. No folder watchers,
polling catalogue refreshes, database frameworks or package-byte caches are used.
Catalogue metadata refreshes on search. Tool arguments are limited to 64 KiB,
HTTP request bodies to 128 KiB, and tool responses to 64 KiB including duplicated
text/structured content. Larger results have a local file; `latest-*` reports are
replaced by the next oversized response from that tool.

[Memory measurements](docs/memory.md) include two HTTP clients, large packages,
repeated sessions and two compatibility processes. Measurements assess growth
and allocations; there is no arbitrary fixed memory cap.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python scripts/measure_memory.py
```

Tests cover all eight tools in process, over real stdio, and over shared HTTP,
including auth/origin rejection, concurrent clients, unchanged consuming checkouts,
package safety/limits, retries, cleanup, frozen binary updates and crash recovery.
Tests and the memory benchmark use temporary libraries and state, not personal skills.
