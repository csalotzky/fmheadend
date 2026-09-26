# Status polling and PCM isolation

## Measured investigation (Pi 3B+, 2026-09-26)

The operator reproduced audible popping and mux PCM underrun/overrun bursts with
WebUI open, including bypass channels. Mux cadence, SPI errors and RP/QN audio
errors remained stable. This is evidence for a Linux-side interaction, not proof
of a particular GIL pause or queue-lock hold.

Before this change, `GET /status` ran on `ControlExecutor` behind maintenance,
RDS and lifecycle commands. It did **not** itself query RP or mux hardware.
`StatusService.snapshot` nevertheless read Source records, reopened/validated
active DSP profile JSON, constructed models and sampled audio diagnostics.

### Timings before editing

Read-only localhost HTTP probes against the existing live daemon:

| Measurement | Wall time |
| --- | ---: |
| One status GET | 963.7 ms |
| Two concurrent status GETs | 1489.7 / 1625.0 ms |

A separate process on the same Pi ran the existing snapshot code with the actual
read-only DB/profile inputs and synthetic runtime/queue objects. It did not
instantiate DSP, connect PCM sockets or execute RP commands. Three runs:

| Phase | Mean wall time | Mean thread CPU time |
| --- | ---: | ---: |
| Complete snapshot collection | 46.60 ms | — |
| Source status (8 Sources; inclusive of rows below) | 40.37 ms | 39.29 ms |
| Source DB reads/model validation | 10.51 ms | 9.99 ms |
| Profile file reading/validation (4 built-in Sources) | 24.97 ms | 24.63 ms |
| Source state locks | 0.33 ms | 0.33 ms |
| Staging/fanout diagnostics | 0.49 ms | 0.50 ms |
| Writer diagnostics | 0.13 ms | 0.13 ms |
| Model JSON-mode dump | 1.00 ms | — |
| HTTP JSON encoding | 1.13 ms | — |

A profile list independently took 18.22 ms. Nested rows are **not additive**.
These isolated figures exclude live lock contention and are not an exact
profile of the live HTTP handler.

A separate, short `strace` attachment to only the existing control thread
confirmed the actual 849.876 ms `clock_nanosleep` for RP GET_STATUS, followed
by the header/payload reads. RDS capacity/refill preceded it. Mux control then
used a separate Unix socket; no PCM ownership lock surrounds this control I/O.
In that trace one request took 2041 ms: 1674 ms elapsed until the maintenance
job completed, the following snapshot job completed 356 ms later, and the HTTP
response completed about 11 ms after that. Tracing substantially inflated Python/
syscall costs; these are ordering evidence, not uninstrumented performance numbers.
No GStreamer/PCM thread was traced.

The measured long request latency is mainly control-executor queueing. Sleeping
for RP response timing does not hold a PCM mutex. Audio interference has a
separate credible mechanism: request-amplified Source/profile/model work and
sampling of locks also used by PCM workers. No measurement in this round proves
a specific GC pause, long-held lock or uninterrupted GIL stall. Live audible
acceptance therefore remains necessary.

## Complete browser request inventory

Same application polling code in Angular development and production builds:

* `DashboardState`: `timer(0, 5000)` + `exhaustMap`, with an in-flight Promise
  coalescing manual refresh. One refresh uses `forkJoin` of `/status`,
  `/modulators`, `/sources`. No second shared refresh while these are pending.
* Shell: `/system/info` and `/audio-profiles/files` initially and after shared
  snapshot changes. These are separate from the shared in-flight Promise.
* Sources route: `/sources/{id}/status` for each listed Source, concurrency four,
  after shared updates. Previous subscriptions are cancelled on update/destroy.
* System route: `/system/info` plus `/system/logs`; logs have a loading guard and
  manual refresh. Subscriptions are cleaned up on destroy.
* Editors/list actions also fetch configuration, profile/DSP metadata, or perform
  explicit tests/mutations. Those are not periodic status polls.

The precise route in the operator's Test 2 was not identified. The implementation
covers the periodic set for all routes, including Sources and System.

`npm start` runs `ng serve --proxy-config proxy.conf.json`. Only `/api/**` is
proxied to localhost:8080 (the Windows SSH forwarding endpoint in this setup).
JS, source maps, development assets and HMR connections stay at the Windows dev
server. HMR/component recreation can restart normal application polling; it does
not introduce a separate Pi hardware poll. Multiple tabs remain independent.
Unsubscription previously did not cancel already submitted shielded control jobs.

## Implemented boundary

The existing single control thread collects and publishes a complete owned
observation generation. HTTP readers reference that generation; they do not
submit control jobs, access SQLite/files, collect Source diagnostics, or acquire
PCM locks. No new thread, queue, worker or hardware polling path was added.

Published endpoints are `/status`, `/sources`, `/modulators`,
`/sources/{id}/status`, `/system/info`, and `/audio-profiles/files`.
`/system/logs` keeps its existing bounded 100-entry in-memory logging-lock snapshot;
that lock is not an audio queue/writer lock. Single-resource editor GETs and
mutations remain on the control executor. Their DB reads do not run hardware
queries or take PCM locks merely to fetch configuration.

Maintenance attempts publication at most once a second after its existing work.
There is no hard scheduling deadline: a slow RP/lifecycle action can delay it.
All mutating control-route completions, including partial failures, publish before
returning. This is observation only: no extra RP or mux-control query. Publication
failure is logged, marks observations stale and cannot fail an already confirmed
RF action. Startup and shutdown ownership/order remain unchanged.

### Explicit freshness

* `/status.system.snapshot_age_s` and `snapshot_stale` describe publication age.
  Freshness limit is `max(2 seconds, 2 * configured RP poll interval)`.
* Expired/publication-failed snapshots retain actual values, set RP observation
  stale, and are treated as stale by the shared UI. They do not invent TX ERROR
  or alter desired/recovery/RF state. RP observation age and daemon uptime advance
  when served, without a hardware query.
* Source status adds `observation_age_s` / `observation_stale`. The Sources page
  does not render expired samples as current RUNNING observations.
* List/info responses retain their JSON shapes and include
  `X-Observation-Age-Seconds` and `Cache-Control: no-store`.
* Profile summaries are refreshed every five seconds of maintenance, or after a
  mutation. Their age header reflects the profile-list sample, not the newer
  overall publication. Missing/invalid profile lists keep their typed API error;
  they are not replaced by an empty success list.
* Read-only Source profile validation is reused for one second per Source and
  selection. Metadata/PCM counters are sampled normally, not from this cache.
  External file edits can take that validation interval plus publication scheduling
  to appear. START/join/test/save still validate immediately; no default/Bypass
  substitution is introduced. Save/rebuild/release invalidate relevant entries.

There is only one current publication and one profile-list sample. Published
nested runtime models are copied and never mutated. A reader sees one generation
per response; independent HTTP responses can straddle generations, as before.

## Verification and remaining live test

Candidate code in an isolated temporary Pi directory used actual read-only
configuration/profile input and synthetic runtime (no application lifespan,
RP commands, PCM producer connections or audio playback). An ephemeral localhost
ASGI server tested the real routes, with collection methods replaced by assertions
that fail if an HTTP read accesses them:

* Cold/warm publication: 77.98 / 34.35 ms wall, 69.53 / 30.24 ms thread CPU.
* First cold HTTP status request: 1206.39 ms. This separate process/server startup
  cost is retained here; it was not attributed to PCM locks.
* Next two simultaneous status requests: 19.35 / 23.67 ms.
* Two 14-request bursts (shared + Shell + System + all eight Source statuses):
  individual responses 40.99–129.42 ms, no collection assertion triggered.

These tests demonstrate HTTP/control/audio-lock separation, not full live DSP
performance. Publication still does bounded synchronous Python work on the owner
thread. Arbitrarily excessive HTTP traffic can still consume CPU/GIL time during
response serialization. The existing audio/DSP load and scheduler margins remain
relevant; this is not a hard real-time guarantee.

After operator deployment, repeat the same 120-second controlled comparisons with
identical DSP/RDS/configuration and existing mux SCHED_RR settings:

1. Browser closed, independent status GET every 1.5 s.
2. Dashboard open in the Windows dev server, same independent GETs.
3. Sources open, then System open, same independent GETs.
4. Production WebUI build, same routes and workload.

Use direct `mux-control.sock` before/after counters, HTTP latency distribution,
all staging/consumer/writer drop counters, RP SPI link and QN audio underruns.
Check snapshot ages/staleness and audible popping too. The existing bounded
`daemon/tools/pcm_status_probe.py` can collect API/counter deltas; keep its cadence
identical between cases. Success requires no steady-state audio loss/pop regression,
not just faster HTTP. No deployment, service restart, scheduling change or RF
acceptance occurred during this implementation.

## Code map and regression results

Control/collection path:

* `daemon/api/routes.py`: periodic readers return published models; editor reads
  and mutations retain `control_route`.
* `daemon/control.py:ControlExecutor.call`: single worker, 32 pending cap,
  shielded already-submitted work; unchanged serialization for side effects.
* `daemon/app.py:tick`: expiry/recovery/RDS, scheduled hardware refresh, then
  throttled publication. No second maintenance task or hardware poller.
* `daemon/status/service.py:refresh`: RP observation plus Unix mux-control query.
  RP uses its existing client RLock and command wait; mux-control socket has a
  0.5-second deadline. Neither holds the PCM writer ownership lock.
* `StatusService.publish/snapshot`: Source inventory read once, one Source
  observation per ID, reuse across fanout TXs, one writer-counter sample, per-TX
  model copies and existing RDS state fields. No RDS scheduling/encoding query.
* `daemon/sources/manager.py:status`: owned Source/config validation and native
  pipeline snapshots. `GstPipeline.snapshot` takes the Source metadata lock;
  `pcm_snapshot` takes staging and output/consumer locks independently.
* `daemon/sources/output.py`: queue snapshots take a small scalar-counter lock;
  output snapshots copy queue references under the output lock, then sample queues.
* `daemon/mux/unix_client.py:pcm_snapshot`: takes the writer ownership lock to
  copy scalar counters. Publication samples it once, independent of reader count.
* `published_snapshot/published_source/published_view`: no dependency access or
  audio locks. FastAPI still validates/serializes the returned response models.

Regression commands executed on Windows:

```powershell
.venv/Scripts/python.exe tmp/run_scoped_pytests.py daemon/tests/test_backend.py daemon/tests/test_recovery.py daemon/tests/test_rp2354.py daemon/tests/test_audio_profiles.py daemon/tests/test_pcm_delivery.py daemon/tests/test_rp2354_audio.py -q
.venv/Scripts/python.exe tmp/run_scoped_pytests.py daemon/tests/test_status_publication.py -q
.venv/Scripts/python.exe tmp/run_scoped_pytests.py daemon/tests -q -ra
cd webui
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
git diff --check
```

* Focused existing backend suites: **368 passed, 1 skipped**.
* New publication/concurrency tests: **6 passed** (also in full suite).
* Final full backend: **643 passed, 28 skipped**, one Starlette/httpx deprecation
  warning. Skips: 21 unavailable GStreamer/PyGObject, 5 Linux native mux integration,
  1 Linux i2c-dev test, 1 unavailable Windows symlink privilege.
* Final frontend: **176 passed, 9 files**; typecheck/build passed.
* Whitespace check passed (Git emitted only its normal LF/CRLF conversion warnings).

Tests use events/held locks rather than sleep-based timing assertions for control
and audio independence. They cover concurrent complete UI polling, retained/stale
observations, immediate mutation visibility, preserved STOP ALL intent, owned
Source models, strict join validation, bounded profile-list reuse/errors,
publication failure after confirmed OFF, and ordinary shutdown cleanup. Existing
frontend tests retain shared refresh coalescing/non-overlap and route destruction.

Changed repository files:

```text
daemon/api/routes.py
daemon/app.py
daemon/control.py
daemon/models/runtime_status.py
daemon/sources/manager.py
daemon/status/service.py
daemon/tests/test_audio_profiles.py
daemon/tests/test_backend.py
daemon/tests/test_pcm_delivery.py
daemon/tests/test_status_publication.py
docs/fmheadend-software-architecture.md
docs/pcm-delivery.md
docs/status-pcm-isolation.md
docs/webui-spec.md
webui/src/app/core/api/models.ts
webui/src/app/features/dashboard/dashboard-state.spec.ts
webui/src/app/features/dashboard/dashboard-state.ts
webui/src/app/features/sources/source-api.ts
webui/src/app/features/sources/sources-list.ts
webui/src/app/features/sources/sources.spec.ts
```
