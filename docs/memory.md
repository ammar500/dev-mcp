# Memory measurements

Measured on 2026-11-09 with Python 3.12.14 on Linux x86-64 using the locked
environment. Reproduce with `uv run python scripts/measure_memory.py`.
[Raw results](memory-results.json) include the environment and configured limits.
If the execution environment has a `/tmp/.git` marker, use `TMPDIR=/var/tmp` so
runtime storage remains outside Git. The benchmark uses disposable data only.

The shared service has the legacy skills endpoint plus personal/work profiles
with tasks and testing enabled. HTTP profiles share one bounded skill pool and
one separate bounded task pool. This run uses two skill HTTP clients, two
workspace HTTP clients, then two compatibility stdio processes. Queue discovery
uses short read-only Git commands; no runner, scheduler, watcher, or additional
resident process was added.

| Observation | Shared server RSS (MiB) |
| --- | ---: |
| Empty service, three configured endpoints | 74.26 |
| Warmed, two skill HTTP clients | 75.21 |
| After 10 / 50 / 100 small skill sessions | 75.52 / 75.65 / 75.75 |
| After two 128 MiB package cycles | 79.63 |
| After publishing a 128 MiB binary edit | 79.64 |
| After 40 further small skill sessions | 79.66 |
| Warmed, two additional workspace clients | 79.79 |
| After 20 / 100 / 200 task/pause cycles | 81.20 / 81.26 / 81.48 |
| After 20,000 history rows and 100 paired queries | 81.60 |
| After 40 further task/pause cycles | 81.60 |
| After 20 / 100 testing join/check/done cycles | 81.84 / 81.85 |
| After 1,000 waiting polls | 82.77 |
| After 20,000 queued tickets and 100 paired list/check queries | 84.12 |
| After 40 further testing cycles | 84.14 |
| HTTP service with two additional stdio processes | 84.14 |
| After clients disconnected | 84.14 |
| Highest sampled RSS during the whole run | 84.20 |

Small skill packages contain a 4 KiB asset; large ones contain a 128 MiB asset.
The two large acquire/release cycles took 0.96 seconds on this machine, not a
portable throughput promise. Publication changes a 128 MiB binary through
streamed copying/hashing. No borrowed package bytes remained after completion;
all consumer and testing checkouts remained unchanged.

Each task cycle creates a task, saves/reads an exact raw note, marks it resumed,
and completes the task. The benchmark inserts 20,000 approximately 4 KiB audit
rows through an iterator and reads bounded pages. The personal task database was
90.74 MiB after this phase; daemon RSS rose 0.12 MiB across population/querying.

Testing cycles use two independent repositories concurrently. A separate shared
repository workload leaves one holder active while the other workspace polls
1,000 times. The holder sends no heartbeat. Polling added **zero request receipts
or audit events**, with 0.92 MiB measured RSS growth. Twenty thousand additional waiting
tickets (4,000 bytes of information each) bring the personal database to
**188.82 MiB**; the daemon gained 1.35 MiB across insertion and bounded queries.
Forty further testing cycles added 0.02 MiB. These finite measurements support
bounded allocation for this workload; they do not prove an absence of all leaks.
The benchmark intentionally leaves unfinished tickets in disposable storage; the
production service never prunes or evicts them.

The two compatibility processes used **73.04 and 73.20 MiB RSS**, in addition to
the shared service. Their sum, 146.24 MiB, double-counts shared pages and is not
unique physical memory. The separate benchmark/client process used 72.76 MiB at
the final sample (74.19 MiB high-water). Real agent applications cost extra. Brief
Git child-process memory is not included in daemon RSS, and this benchmark does
not claim a peak for those children. There is no persistent Git subprocess.

The [2026-10-16 task-feature run](memory-results-2026-10-16.json) measured 72.52 MiB
empty idle and 75.75 MiB after completion, with a 77.58 MiB sampled peak. The
2026-10-12 skills-only run measured 71.37 MiB empty idle and a 76.39 MiB peak.
These are separate workloads/environments, not controlled attribution to this
feature: this run has a different Python/host environment, four additional tools
per enabled profile, more mutations, and a large pending queue. The historical
approximately 66 MiB import/setup peak was not daemon-idle memory.

Packages, context, histories, tickets and request receipts stay on disk. SQLite
connections/cursors close after operations; there is no backlog-sized application
cache. Queue operations select indexed heads and bounded pages. Profile tool
schemas remain in memory, bounded by the profile limit; larger catalogues still
have memory and agent-context cost. Testing tools are opt-in.

RSS/HWM come from `/proc/<pid>/status`, sampled every 20 ms. Sampling can miss short
spikes. Allocator behavior, shared pages, filesystem caches and disk usage affect
interpretation. The older run reported decreasing VmHWM fields, so compare maximum
observed samples rather than treating a final counter as an exact peak. No arbitrary
total-memory cap has been agreed. Repeat measurements when changing the SDK,
indexing, admission pools, response handling, catalogue size, or retention.
