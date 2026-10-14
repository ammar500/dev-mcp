# Memory measurements

Measured on 2026-10-12 with Python 3.12.3 on Linux x86-64, using the locked
environment. Reproduce with `uv run python scripts/measure_memory.py`.
[Raw results](memory-results.json) include the environment and configured limits.

The benchmark runs a real shared HTTP server with two clients, 100 small
acquire/release sessions, two 128 MiB package cycles, a 128 MiB binary publication,
40 further small sessions, and two additional compatibility stdio processes. It
uses temporary sample skills and state; it does not touch the personal library.

| Observation | Server RSS (MiB) |
| --- | ---: |
| Empty service, idle | 71.37 |
| Warmed, two HTTP clients connected | 71.91 |
| After 10 small sessions | 72.19 |
| After 50 small sessions | 72.45 |
| After 100 small sessions | 72.55 |
| After two 128 MiB package cycles | 76.32 |
| After publishing a 128 MiB binary edit | 76.36 |
| After 40 further small sessions | 72.62 |
| After clients disconnected | 72.62 |
| Highest sampled RSS during the run | 76.39 |

Small packages contain a 4 KiB asset and short instructions. Large packages
contain a 128 MiB asset and short instructions. Source mutation and durable
filesystem operations are serialized, even with two connected clients. The two
large acquire/release cycles took 0.92 seconds on this machine; this is not a
portable throughput promise.

Two separate stdio compatibility processes used **71.43 and 71.53 MiB RSS**.
Their approximately 142.96 MiB summed RSS is additional to the shared server's
72.62 MiB at that point. RSS sums count shared pages more than once; they are
not measurements of unique physical memory. The separate benchmark process,
containing both HTTP clients and the stdio client endpoints, used 67.97 MiB RSS.
Existing agent clients have their own memory costs beyond these measurements.

The small-session series grew about 0.64 MiB from warmed idle through 100 sessions.
Peak observed growth over warmed idle was about 4.48 MiB despite handling 128 MiB
assets. After further cycles, the service was within 0.71 MiB of warmed idle.
No borrowed package files remained, both consuming checkouts were unchanged,
and completed publication staging/backups were disposable and removed. Durable
metadata and audit history remained in SQLite rather than a process-side history
cache. These results support the initial bounded-allocation/growth criterion.

This is one finite workload, not a proof against every long-running leak or a
bound for every possible metadata/file-count distribution. File manifests are
bounded by the configured file/path limits; packages with thousands of files can
use more metadata memory than these binary-heavy examples. Repeat measurements
when changing the SDK, indexing, concurrency, response handling, or retention.
There is no agreed arbitrary total-memory cap.

RSS and high-water counters come from `/proc/<pid>/status`, sampled every 20 ms.
They are approximate, may miss short spikes, and allocator/page-accounting behavior
can affect individual observations. The reported peak is the largest sample,
not simply the final high-water-counter reading. Filesystem cache and disk use
are separate costs. The earlier approximately 66 MiB import/setup peak was a
preliminary measurement and should not be treated as daemon-idle memory.
