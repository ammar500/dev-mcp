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

- `dev-mcp serve` runs one loopback Streamable HTTP process at `/mcp`, default port
  8765. Direct HTTP is the recommended multi-client configuration.
- HTTP uses a private local bearer token and the SDK's Host/Origin protection.
  Never log, return or commit credentials. The shared token represents one owner;
  client/task labels are assertions, not independent authentication.
- `dev-mcp service install|start|stop|status` manages a user-level systemd service.
  Installation enables login startup/restart on failure. Never enable a system-wide
  service or lingering. Never silently start fallback servers for clients.
- Stdio remains a compatibility transport and starts a separate full process per
  client. Stdout must stay protocol-only; diagnostics go to stderr.

## Memory and operating cost

Low idle resource use and bounded growth are primary requirements. Include memory
cost in architecture discussions and feature work.

- Packages/binary assets, immutable proposals, backups and durable history belong
  on disk. SQLite holds metadata; no database frameworks or vector search.
- Stream copying/hashing in 1 MiB chunks. Never return binary packages as base64.
  Do not retain completed histories or package bytes in process caches.
- Default limits: 256 MiB/4,096 files per package, 8,192 filesystem entries,
  2 GiB managed package storage, two file workers and sixteen waiting operations.
  Shared admission and state locks coordinate HTTP and stdio processes.
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

## Validation and maintenance

Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
`uv run pytest`. Protocol tests exercise real subprocess stdio and localhost HTTP;
they need local socket/process access. Use temporary libraries/state for tests,
not personal skills. Cover ownership, retries, stale revisions, interrupted
publication and concurrency for relevant changes. Re-measure memory after changes
that could affect allocation or retention.

Keep `AGENTS.md` and `CLAUDE.md` identical. Keep README, recovery procedures and
memory observations aligned with implemented behavior; do not describe deferred
native installation or remote delivery as existing features.
