# PCM delivery investigation and bounded forwarding

## Evidence and scope

The production path remains:

`GStreamer streaming callback -> staging queue -> source worker -> per-consumer
queue -> shared Unix writer -> muxd channel ring -> fixed-rate frame builder`.

The callback copies one whole, frame-aligned buffer, bounded by staging capacity
(38,400 bytes by default). It
does not do socket/hardware IO. One source still has one decoder/DSP worker and
independent consumer queues. All PCM remains interleaved S16LE/48 kHz/stereo.

The reported hardware run has stable mux frame cadence/SPI and RP/QN counters
while Linux PCM under/overruns burst, including bypass channels. This points to
delivery before the SPI pump. It does not identify Python/GIL as the sole cause:
`late_frames` measures the mux pump, not the scheduling latency of its separate
Unix receiver thread. Average host CPU also does not measure per-core stalls.

| Hypothesis | Evidence / qualification |
|---|---|
| GstBus wait delays staging | Confirmed: worker waited up to 10 ms before draining PCM. Pi localhost MP3 probe measured ~5 ms mean/~10.1 ms maximum residence. |
| Lock contention discards PCM | Confirmed policy in old queue, even enforced by an old test. Frequency in the operator's load run was not measured. A discarded 4,608-byte MP3 block is 24 ms. |
| Writer polling causes bursts | Fixed 2 ms sleep/one-send rounds unnecessarily slowed catch-up. They were not a throughput limit for the measured 24 ms buffers. |
| GIL/GC is the root cause | Plausible, **not established**. No GC tuning, thread-priority change or new worker architecture is justified by the current evidence. |
| Additional concrete hazard | `_connect()` could wait 100 ms while the sole writer held its ownership lock, delaying every channel. Relevant during reconnect, not proof of the steady-state failure. |

Queue capacities are **ceilings**, not guaranteed prebuffered headroom.
Native decoder buffer sizes vary by codec; the 4,608-byte measurement is not a
new chunk-size contract. Splitting it into 512-byte chunks cannot make decoded
samples arrive earlier, and is not needed by muxd's existing stream assembler.

## Implemented change

* Queue insertion takes the same short metadata lock as removal. No IO, external
  callbacks, downstream locks or capacity waits occur inside it. Work is bounded
  by the configured queue capacity. Contention alone no longer drops blocks.
  This is bounded work, **not** a hard realtime mutex latency guarantee under OS
  preemption. Capacity eviction and 100 ms stale removal remain intentional.
* The appsink callback records caps/time **before** publishing PCM and waking the
  worker. Otherwise a promptly awakened worker could report ready with no caps.
* The source worker polls GstBus nonblocking, at most 32 messages per pass, and
  drains up to 16 PCM blocks. Queued startup headers/tags are serviced before
  readiness; PCM drains between bus batches. It waits on a PCM/stop Event only
  when idle; a 50 ms idle ceiling services bus errors without audio, capped by
  the remaining audio-stall deadline. PCM and stop still wake it immediately.
* Negotiated output caps are validated once and revalidated when changed;
  missing/incompatible caps still fail. Input metadata is reused until caps
  change. Cached GI caps references are reset on every pipeline attempt.
* Event clearing precedes checking/draining work, avoiding lost notifications.
* The mux writer receives consumer-queue notifications. It performs at most 16
  fair rounds per pass, one nonblocking send per channel per round, releasing
  its ownership lock between rounds. No additional threads were introduced.
* Unix connection attempts are also nonblocking. A refused/full/in-progress
  connection is closed and retried with the existing 250 ms backoff. Idle worker
  wakeups are at most 50 ms apart; pending EAGAIN writes retain a 2 ms retry wait.
* Partial writes retain exact byte order. After 100 ms pending, the session is
  closed before discarding bytes so muxd cannot splice partial stereo frames.
* Stop/close wakes idle waits. Source hot-save retains its existing shared
  `PcmOutput` object and consumer notifications. No lifecycle/recovery policy was
  changed. No TX, QN, RDS, firmware, DSP or SPI code was changed.

Queue age bounds remain per queue, not a promised end-to-end latency.
Kernel buffering and the mux ring also contribute; prolonged stalls intentionally
discard audio rather than accumulating unbounded backlog.

## Runtime buffer controls

| Process / option | Default | Meaning |
|---|---|---|
| fmheadendd `--pcm-staging-buffer-ms` | 200 | Byte capacity / burst headroom only; stale age stays **100 ms** |
| fmheadendd `--pcm-consumer-buffer-ms` | 300 | Independent queue per consumer: both byte capacity and maximum age |
| fmheadend-muxd `--ring-buffer-ms` | 300 | Per-channel ring byte capacity; discard oldest on overflow, zero-fill on underrun |

At S16LE / 48 kHz / two channels, capacity = integer milliseconds × 192 bytes,
always stereo-frame aligned. Defaults are 38,400 staging bytes and 57,600 bytes
per consumer/ring. Values must be positive integers and fit the implementation's
byte-container size; there is no arbitrary tuning maximum. A mux ring smaller
than a 512-byte payload is allowed and naturally produces partial silence.
Effective values are logged once at startup. Settings are instance-owned, including
new source attempts and audio-only rebuilds; global `PcmQueue` defaults stay
19,200 bytes / 100 ms.

Staging headroom absorbs measured compressed PCM arrivals; it does not change
the 100 ms stale policy or intentionally prebuffer 200 ms. The final defaults
are deliberate production values, replacing experimental hard-coded buffers.

The measured loaded Pi configuration requires **both muxd threads SCHED_RR 10**;
Python, including `mux-pcm-output`, remains SCHED_OTHER. Restore and verify this
after a live muxd restart. Persistent privileges/service scheduling remain a
deployment item: these code changes do not modify systemd or acquire RT privileges.

Upstream semantics: [appsink streaming callbacks](https://gstreamer.freedesktop.org/documentation/app/appsink.html),
[GstBus timed_pop](https://gstreamer.freedesktop.org/documentation/gstreamer/gstbus.html#gst_bus_timed_pop),
[Python socket blocking modes](https://docs.python.org/3/library/socket.html#notes-on-socket-timeouts).

## Additive diagnostics

`GET /api/v1/status` retains all prior fields and adds:

* system: `mux_late_frames`, `mux_transport_errors` from existing mux counters.
* per TX: `pcm_staging`, `pcm_consumer`, `pcm_writer`; null when unavailable.
* Each queue: `accepted_bytes`, `buffered_bytes`, `dropped_blocks`,
  `capacity_dropped_bytes`, `stale_dropped_bytes`, `invalid_dropped_bytes`,
  `cleared_bytes`. Clears are intentional lifecycle discard, separate from loss.
* Writer: `disconnects` (includes failed connection attempts),
  `stale_disconnects`, `discarded_pending_bytes` (unsent remainder only).

`GET /sources/{id}/status` exposes the same queues under `pcm.staging` and
`pcm.consumers`. Staging counters are shared by every TX using that source:
**do not sum duplicated staging counters across fanout TXs**. Consumer counters
are independent. There is no lock-contention-loss counter because that loss
policy no longer exists. Native appsink/GStreamer-internal loss is not counted
by Python queue counters; source errors retain their existing diagnostics.

Snapshots have bounded locking and no audio-path logging. Python counters are
live snapshots; mux/RP observations retain their existing refresh cadence and
RP freshness flags. These are not one simultaneous distributed measurement.
Counters last for a queue/producer object's lifetime. Source replacement resets
staging counters; detach/reattach resets consumer/writer counters. Reconnect clears
queues without resetting their counters. Restart measurement windows after edits.

## Reproducible isolated probe

With this code available in a non-production package workspace and native GI:

```sh
python -m fmheadendd.tools.pcm_delivery_probe
# Optional: read an existing validated profile without modifying it:
python -m fmheadendd.tools.pcm_delivery_probe --profile /absolute/path/profile.json
```

This creates a temporary localhost MP3 fixture, tests Bypass and Built-in
sequentially for four seconds each, reports callback size/cadence and staging
residence, then closes all pipelines. It does not connect to muxd or RP hardware.
The default Built-in case uses the current neutral stage configuration, not the
operator's tuned LSP preset. Tests use isolated files, not production deployment.

## Operator full-stack validation (pending)

After the operator separately installs the change, retain the **same already
tested mux scheduling arrangement** throughout all cases. Do not compare Python
changes while also changing mux scheduling. Check every mux thread read-only:

```sh
pid=$(pgrep -x fmheadend-muxd)
ps -L -p "$pid" -o pid,tid,cls,rtprio,comm
```

From the repository root, use the existing UI Source hot-save operation to choose
each case, without changing the tuned profile: A eight Bypass, B one Built-in,
C four Built-in, D five Built-in, E eight Built-in. Keep eight **distinct** sources
and eight active consumers. Allow startup/rebuild transients to settle (at least
10 seconds with RUNNING sources) before starting each separate window. These
commands only observe; they neither configure nor start/stop transmitters:

```sh
python3 daemon/tools/pcm_status_probe.py --label A-bypass --seconds 120 > /tmp/pcm-A.jsonl
python3 daemon/tools/pcm_status_probe.py --label B-one    --seconds 120 > /tmp/pcm-B.jsonl
python3 daemon/tools/pcm_status_probe.py --label C-four   --seconds 120 > /tmp/pcm-C.jsonl
python3 daemon/tools/pcm_status_probe.py --label D-five   --seconds 120 > /tmp/pcm-D.jsonl
python3 daemon/tools/pcm_status_probe.py --label E-eight  --seconds 120 > /tmp/pcm-E.jsonl
tail -n 1 /tmp/pcm-E.jsonl | python3 -m json.tool
```

Run **one command after configuring its corresponding case**, not all five
against the same source configuration. Override `--url` if not localhost:8080.
The sampler stores no growing in-memory history: each line includes raw status,
interval deltas and total deltas. Null means unavailable/reset, never success.
A fetch failure exits visibly. A stale RP observation is not proof of zero errors.
Cached mux counts can repeat between refreshes; expect ~375 frames/s over the
whole window, allowing the existing refresh phase offset. For sub-refresh timing,
use the mux's existing read-only control status alongside this sampler.

Healthy steady-state targets:

* `mux_late_frames`, transport/SPI errors and RP audio-link error deltas zero.
* Queue capacity/stale/invalid loss and writer discarded bytes zero.
* Each active TX's mux under/overrun and QN audio underrun deltas zero.
* No monotonically growing queue occupancy/stale playback, no source reconnects
  or unexpected lifecycle changes. Lifecycle transients belong outside the window.

If bursts remain, correlate stage/consumer loss versus writer disconnects. If
all Python loss counters stay zero but all mux inputs starve, measure producer
arrival gaps and mux receiver scheduling before blaming DSP/GIL or enlarging
buffers. This patch is not claimed to prove eight live tuned DSP chains reliable.

## Mux realtime policy

The current systemd unit sets no realtime policy. Persistent SCHED_RR is a
**separate follow-up change**: the operator's priority 10 result supports testing
that narrow policy for muxd only (both receiver and pump), not Python/GStreamer.
The systemd privilege/RT-limit and fallback behavior must be established before
shipping such a unit. Avoid a higher-priority pump starving the receiver that
fills its buffers; do not invent a new priority hierarchy here. This task leaves
service files and runtime scheduling unchanged.

## Verification — 2026-09-26

* Windows full backend: **636 passed, 28 skipped**. Skips: 21 native GStreamer,
  five Linux mux integration, one Linux i2c-dev open-failure test and one Windows
  symlink-privilege test. One existing Starlette/httpx deprecation warning.
* Isolated Pi (aarch64, GStreamer 1.26.2): **102 passed, one skipped** across
  PCM/media/mux-client/GStreamer/modular-DSP/mux-integration suites. The only
  skip was ALSA capture without a selected device. Two dependency deprecations.
* Unchanged C++ mux Release build succeeded; **8/8 CTest tests passed** including
  Unix sockets, with mock transport only.
* `compileall` and `git diff --check` passed. No backend lint/typecheck tool is
  configured in this repository. No frontend implementation changed.
* Before-change neutral MP3 probe: 4,608-byte blocks every ~24 ms, staging means
  5.01/5.14 ms and maxima 10.09/10.07 ms (Bypass/Built-in), zero staging loss.
* Initial event-wakeup probe: means 0.23/0.18 ms, maxima 0.60/0.71 ms, zero loss.
  Native tests then exposed the caps-publication/readiness race; it was corrected
  and the native suite rerun successfully, not dismissed as a timing-test failure.
* Final-code probe using the existing tuned file read-only:
  `/var/lib/fmheadend/audio-profiles/custom/teszt_20260925092851.json`.
  Bypass/Built-in means **0.33/0.28 ms**, maxima **4.90/5.64 ms**, zero staging
  and consumer loss, 4,608-byte blocks/~24 ms. This includes startup scheduling;
  maximum residence is not a hard deadline or a guarantee under eight-source load.

Test dependencies pytest/httpx and their dependencies were installed only inside
the isolated temporary workspace, not the production venv. No production service,
database, profile, RT policy or hardware state was changed. No physical RF/audio
acceptance or eight-live-source/tuned-DSP load success is claimed. The full-stack
matrix above remains the operator's next validation step.

## WebUI/status-plane follow-up

The separate investigation of audible popping correlated with WebUI polling,
request timing, complete endpoint inventory and published observation semantics
is recorded in [Status/PCM isolation](status-pcm-isolation.md). No PCM queue,
writer, DSP, mux scheduling or hardware-lifecycle change is part of that follow-up.
