# Memory measurements

Measured on 2026-10-16 with Python 3.12.3 on Linux x86-64 using the locked
environment. Reproduce with `uv run python scripts/measure_memory.py`.
[Raw results](memory-results.json) include the environment and configured limits.

The shared service has the legacy skills endpoint plus personal/work task profiles.
HTTP profiles share one bounded skill pool and one separate bounded task pool;
adding a profile does not allocate another executor. This run uses two skill HTTP
clients, then two concurrent workspace task clients, and finally two additional
stdio processes (legacy skills and personal tasks). Everything runs with temporary
libraries/state; no personal data or real consuming checkout is modified.

| Observation | Shared server RSS (MiB) |
| --- | ---: |
| Empty service, three configured endpoints | 72.52 |
| Warmed, two skill HTTP clients | 73.07 |
| After 10 small skill sessions | 73.37 |
| After 50 small skill sessions | 73.64 |
| After 100 small skill sessions | 73.73 |
| After two 128 MiB package cycles | 77.50 |
| After publishing a 128 MiB binary edit | 77.54 |
| After 40 further small skill sessions | 73.80 |
| Warmed, two additional workspace clients | 73.89 |
| After 20 task/pause cycles | 75.14 |
| After 100 task/pause cycles | 75.30 |
| After 200 task/pause cycles | 75.40 |
| After 20,000 durable history rows and 100 paired history/list queries | 75.73 |
| After 40 further task/pause cycles | 75.75 |
| HTTP service with two additional stdio processes | 75.75 |
| After all clients disconnected | 75.75 |
| Highest sampled RSS during the run | 77.58 |

Small skill packages contain a 4 KiB asset; large packages contain a 128 MiB asset.
The two large acquire/release cycles took 0.94 seconds on this machine, not a
portable throughput promise. Publication also modifies a 128 MiB binary using
streamed copying/hashing. No borrowed package bytes remained after completion.

Each task cycle creates a task with instructions, saves and reads an exact raw
pause note, marks it resumed, then completes the task. Two workspaces operate
concurrently. The benchmark additionally writes 20,000 approximately 4 KiB audit
records through SQLite using an iterator, then reads bounded history/list pages.
The personal task database measured **90.77 MiB**; daemon RSS increased 0.33 MiB
across the history population/query phase, rather than retaining the history's
bytes. After the first 20 task cycles warmed the mutation paths, another 220 cycles
and the large-history workload added 0.61 MiB. This is observed bounded growth for
this workload, not a mathematical proof of no leak.

The two compatibility stdio processes used **71.39 and 71.92 MiB RSS**, additional
to the shared server. Their summed RSS is 143.31 MiB; sums double-count shared
pages and are not unique physical-memory measurements. The separate benchmark
process containing the clients and fixture generator used 70.74 MiB RSS. Actual
agent applications and future execution processes have additional costs.

The earlier skills-only run on 2026-10-12 measured 71.37 MiB empty idle, 71.91 MiB
warm idle, and a 76.39 MiB sampled peak. The new empty-service reading is 1.15 MiB
higher with two task profiles initialized. These are separate runs with different
workloads and allocator state, not a controlled attribution of every allocation.
The historical approximately 66 MiB import/setup peak was not daemon-idle memory.

Peak growth over the initial warm idle was 4.51 MiB while handling 128 MiB files.
Final RSS was 2.68 MiB above initial warm idle after also exercising task mutations
and a large history. Package contents, context files, completed history and retry
receipts remain on disk. SQLite connections and cursors close after operations;
there is no backlog-sized application cache. Profile tool schemas are retained,
bounded by the configured profile limit, so larger catalogues/profile counts still
have a measurable memory and agent-context cost.

RSS and high-water counters come from `/proc/<pid>/status`, sampled every 20 ms.
Sampling can miss short spikes. The host's reported `VmHWM` decreased in later
samples in this run; use the maximum observed RSS/HWM in the raw report, not the
final high-water field as an exact peak. Allocator behavior, shared pages, filesystem
cache and disk usage affect interpretation. These measurements cover one finite
workload, not all possible metadata shapes, 64-profile deployments, or very large
file manifests. No arbitrary total-memory cap has been agreed. Repeat measurements
when changing the SDK, indexing, admission pools, response handling or retention.
