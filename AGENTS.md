# Repository guidance

## Project scope

- `dev-mcp` is a general developer MCP toolbox. Skill management is one independent
  feature in `src/dev_mcp/skills/`, not the whole product.
- Keep tools independent of Codex, Claude, or any other client. Native installation,
  remote delivery and creating/deleting entire skills are outside v1.
- The service lives on this computer. Cloud hosting is not a near-term goal.
- Preserve the separate configuration, storage/admission, CLI, transport and feature
  modules. Do not introduce plugin frameworks or preload future heavy features.

## Background runtime

- `dev-mcp serve` runs one loopback Streamable HTTP process, default port 8765.
  `/mcp` is the preserved legacy personal skills-only endpoint; `/mcp/<profile>`
  binds exactly one workspace/profile. Direct HTTP is recommended for multiple clients.
- HTTP uses a private local bearer token and the SDK's Host/Origin protection.
  Never log, return or commit credentials. Each profile credential has a fixed scope;
  the legacy token retains personal skills access only. Client/session labels remain
  assertions, not individual authentication. Owner CLI alone configures access.
- `dev-mcp service install|start|stop|status` manages a user-level systemd service.
  Installation enables login startup/restart on failure. Never enable a system-wide
  service or lingering. Never silently start fallback servers for clients.
- Stdio remains a compatibility transport and starts a separate full process per
  client and requires explicit `--profile NAME` (`legacy` preserves old skill access).
  Stdout must stay protocol-only; diagnostics go to stderr.

## Memory and operating cost

Low idle resource use and bounded growth are primary requirements. Include memory
cost in architecture discussions and feature work.

- Packages/binary assets, immutable proposals, backups and durable history belong
  on disk. SQLite holds metadata; no database frameworks or vector search.
- Stream copying/hashing in 1 MiB chunks. Never return binary packages as base64.
  Do not retain completed histories or package bytes in process caches.
- Default limits: 256 MiB/4,096 files per package, 8,192 filesystem entries,
  2 GiB managed package storage, two file workers and sixteen waiting operations.
  Shared admission and state locks coordinate HTTP and stdio processes. HTTP profiles
  share bounded thread pools. Tasks have separate admission/short transaction locks;
  task operations must not wait behind long skill copies or context imports.
- Serialize durable filesystem mutations; capacity errors must be actionable.
  Never silently evict active/unfinished work to fit a limit.
- Refresh catalogue metadata only on demand. No watchers, periodic scans, or
  startup hashing of every skill. Startup recovery inspects pending journals.
- Keep tool arguments and serialized responses bounded to 64 KiB; HTTP bodies to
  128 KiB. Paginate search/session results, default 20 and maximum 50.
- Avoid heavy eager imports; lazy imports alone do not reclaim used memory.
  Consider bounded short-lived workers for future heavy features. Count workers,
  compatibility servers and client overhead separately.
- Measure empty/warm idle, peak I/O, post-completion and repeated-cycle memory,
  including concurrent clients. Use `scripts/measure_memory.py` and record actual
  results in `docs/memory.md`; no arbitrary total-memory cap has been agreed.
- The historical 66 MiB import/setup peak was not daemon-idle memory. Distinguish
  observations, estimates, high-water counters and sampling limitations.
- Keep the agent-context cost of growing tool catalogues in future design decisions.

## Skill-management invariants

- Originals live in `skills/<skill-id>/`, with standard `SKILL.md` metadata and
  supporting relative files. The directory ID is stable; `name` must match it.
  The repository starts with an empty library. Never auto-migrate personal skills.
- Every acquisition gets its own private complete copy in XDG state, outside all
  consuming checkouts. No hard links or shared writable copies. Revisions hash
  relative paths, contents and executable flags.
- Track sessions, copies, manifests, proposals, requests and audits in SQLite.
  Contents stay on disk. Runtime/configuration paths must be outside Git.
- Use caller request IDs for creation/update retries. Distinct checkouts/worktrees,
  clients and tasks remain distinguishable; lookup must recover lost session IDs.
- Issuing a copy does not prove it was read, executed or natively installed.
  Agents explicitly use returned local paths and write outputs elsewhere. If a
  sandbox denies access, document the needed access; never copy into a repo silently.
- Never execute skill scripts or install their declared prerequisites in the server.
- Release only matching owned files/directories. Preserve edits, unexpected files,
  replaced inodes, symlinks and unresolved work. Repeated release is harmless;
  sessions with unresolved copies remain open. Published content may become an
  accepted cleanup baseline, but later/unpublished edits remain protected.
- Disconnects, restarts and age never authorize deletion of issued copies.
- Updates require an explicit file list, quoted human instruction and reason.
  The quote is an agent assertion, not independent proof. Record that limitation,
  client/session, affected paths and revisions. No second approval step is required.
- Freeze proposals on prepare; later borrowed edits cannot change them. Validate
  metadata and reject traversal, links, special files, skill-ID changes and
  deletion of required `SKILL.md`. Handle binary files without loading them wholesale.
- Compare source revisions at preparation/application, serialize publication, and
  journal backups for startup recovery. Never overwrite unrelated external edits
  during rollback; report/block conflicts. Local owner recovery is explicit.
- Never automatically stage, commit or push library edits. Delete disposable
  staging only when safe; retain compact audits and discoverable unfinished work.

## Workspaces, tasks, reminders and pause checkpoints

- Keep `access.py`, transport, owner CLI, and `tasks/` feature modules separate.
  Workspaces have private XDG state databases with WAL and explicit migrations.
  No credentials belong in databases, tool outputs, logs, or execution specifications;
  use credential references. Context contents and future logs belong on disk.
- Scope every operation, ID, history and context read to the authenticated binding.
  Expose only permitted tools; task-edit permission does not grant delegation/worker
  access. Worker profiles contain only worker permission. Do not allow scope switches
  through arguments. Recheck profile revocation/changes for HTTP and stdio operations.
- Preserve the legacy `/mcp` library, IDs, token and storage paths. Work starts empty;
  non-personal enabled skill libraries/state are workspace-local. Never auto-migrate.
  Document that unrestricted same-account filesystem access bypasses MCP boundaries.
- Task states are open/in_progress/blocked/done/cancelled; archival is separate and
  reversible. No permanent task deletion. Require request IDs for every task-feature
  mutation, scoped by workspace/profile/operation, and expected versions for edits.
  Identical retries return the original result; changed arguments conflict.
- Task creation/read/claims launch nothing. Existing agents use their own capabilities.
  Coordinate claims/delegations transactionally; generic edits cannot bypass ownership.
  Reserve overlapping checkout paths across workspaces. Keep unavailable reservations
  opaque to other workspaces. No task operation modifies a consuming checkout or Git.
- Reminders are query-only, task-linked, separately acknowledged/cancelled records.
  No scheduler/notifications. Require offset timestamps, validate IANA zone/DST,
  reject unintended past times, preserve UTC interpretation and zone. Default timezone
  Europe/Berlin. Task due, reminder time and execution not-before remain distinct.
  Completing/cancelling/archiving cancels pending reminders atomically; restoring does
  not reactivate them. Reading does not acknowledge or complete anything.
- Pause checkpoints preserve exact raw notes and optional task/checkout/session links.
  Corrections create a new note referencing the old one. Never overwrite raw text.
  Recent unresolved lookup keeps different contexts separate. Reading does not resume;
  save/resume never changes task state, starts/stops agents, or enqueues work.
  **Do not inspect API keys, invoke models, or launch normalization agents.** Optional
  API/agent normalization and proactive notifications are explicitly deferred.
- Managed context snapshots are immutable, streamed, workspace-bound and validated.
  Owner CLI imports files; tools cannot fetch arbitrary server paths. Inline snapshots
  differ from retrieval-time references. Preserve interrupted imports; never silently
  reuse them as ready content. Context copies do not hold task transaction locks.
- Delegation requires both explicit permission and workspace enablement; disabled by
  default. Freeze the selected task version/context/profile/not-before in a queue.
  Starting MCP never starts a worker. The restricted claim/heartbeat/finish contract
  ships with fake-worker tests only; Codex/Claude execution adapters are deferred.
- Persist attempts before issuing them. Current worker concurrency is one globally;
  future polling must be bounded/configurable. Lost/expired ownership becomes uncertain,
  never automatically requeued. Keep checkout/worker reservations until terminal report
  or explicit owner reconciliation. Cancellation requests do not prove stopping.
- Back up task databases via SQLite backup plus managed context; preserve configuration,
  credentials and skill state separately for full recovery. Restore only into an absent
  workspace with workers/clients stopped. Mark restored active attempts uncertain and
  verify context checksums. Never overwrite newer/unfinished work or downgrade schemas.

## Validation and maintenance

Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
`uv run pytest`. Protocol tests exercise real subprocess stdio and localhost HTTP;
they need local socket/process access. Use temporary libraries/state for tests,
not personal skills. Cover ownership, retries, stale revisions, interrupted
publication and concurrency for relevant changes. Re-measure memory after changes
that could affect allocation or retention.

Keep `AGENTS.md` and `CLAUDE.md` identical. Keep README, recovery procedures and
memory observations aligned with implemented behavior; do not describe deferred
native installation, remote delivery, production runners, normalization or proactive
notifications as existing features.
