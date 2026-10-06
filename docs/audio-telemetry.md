# Local audio/DSP telemetry

Optional Linux diagnostics, disabled unless a socket path is supplied:

```sh
fmheadendd --audio-telemetry-socket /run/fmheadend/audio-telemetry.sock --audio-telemetry-hz 50
```

The parent directory must already exist and be writable by the daemon. Rates must
be finite and between 10 and 100 Hz; the default is 50. This is best-effort sampling,
not a realtime guarantee. No browser API, client tool or DSP parameter
editing is provided.

## Ownership and transport

One AF_UNIX SOCK_STREAM client receives one batched NDJSON record per service tick.
Additional clients are closed. A blocked write times out after 250 ms; there is no
application backlog of samples. Disconnects disable metering; clients can reconnect.
The socket is mode 0600. A locked `.owner` sidecar records the application's socket
inode. Startup only removes a matching, same-user, non-listening stale socket;
unrelated paths and active sockets are refused. Normal shutdown removes the socket.
The small ownership marker remains to avoid lock-file replacement races.

`SourceManager` captures current wrapper references and sorted consumer TX IDs under
a short registry lock, then reads each wrapper's Python cache after releasing it.
The asyncio export path calls this directly, independently of `ControlExecutor`;
only enable/disable requests use the control executor. Pipeline start/stop, cache
copying, JSON serialization and socket writes never hold the registry lock.
Emission uses monotonic deadlines, excluding processing time from the normal
period and discarding missed deadlines without catch-up bursts. No full status
collection is performed.
Records are per Source, not per TX; consumer IDs describe attachment, not RF health.
Source names come from pipeline construction and can lag a name-only edit until
rebuild. Processor mode and profile path describe the actual constructed pipeline.

## Sampling and lifetime

The existing `source-<id>` worker alone owns live stage references, meter-enable
changes and property reads. `dsp_stages` reports its actual constructed elements;
OFF stages are absent and validation-only elements are never registered.

The worker checks a monotonic deadline after bounded bus/PCM work, including the
full PCM-batch continuation path. Its normal wait is capped by that deadline.
Delayed passes are not replayed. No sampling thread or streaming-callback work is
added. With no client, no meter properties are read or enabled.

Samples are ordinary Python data, published under the existing short snapshot
lock after all native reads. Native rebuild increments `generation` within a
wrapper. Cache/reference invalidation precedes native teardown; temporary NULL
recovery invalidates the cache but retains the same elements. Wrapper replacement
starts its own generation counter; consumers resolve the current wrapper each
tick. Release/shutdown remove ownership. `sample_monotonic_ns` also distinguishes
samples across wrapper replacement.

Unavailable samples contain `telemetry_available: false` and `generation`, with no
old meter data. Bypass also provides the two endpoint peak meters, with empty `stages`.
Each available sample contains `sample_monotonic_ns`, `collect_duration_ns` and
`stages`, keyed by the existing DSP stage names. Each stage has `implementation`,
the actual `factory`, raw `values`, and optional compact `errors`. Non-finite or
unreadable properties are null with an error; JSON never contains NaN/Infinity.

Meter values are not converted to dB, percentages or LUFS. Custom plugin dB/dBFS
values retain their native units; `clip-count` remains the cumulative uint64 integer.
Static gain/trim/filter stages are not polled. Multiband includes exactly
`len(crossovers_hz) + 1` bands, starting at band 0, independently of compressor bypass.

Only Autogain's `e-g`, `e-in-s`, `e-in-l`, `e-out-s`, `e-out-l` are toggled; their
prior values are restored on disconnect or teardown. No other visibility controls
are changed, and no profile configuration is persisted by telemetry.

## Absolute input/output peaks

Two pass-through GStreamer `level` elements measure short-interval stereo peak dBFS:

- `input`: after decode, conversion/resampling and common stereo caps, immediately
  before Source Gain. It excludes Source Gain, Profile Input Gain and all DSP stages.
- `output`: after the full DSP chain including Output Trim, final S16LE conversion,
  48 kHz stereo PCM caps and the queue, immediately before appsink. It measures the
  final digital PCM produced by the Source, not RF or downstream hardware levels.

These appear once per Source outside `stages`:

```json
{"input":{"peak_l_dbfs":-18.064,"peak_r_dbfs":-18.064},"output":{"peak_l_dbfs":-24.066,"peak_r_dbfs":-24.066}}
```

The interval follows the requested telemetry rate (20 ms at 50 Hz). The Source
worker consumes `level` bus messages' `peak` arrays; no Python PCM calculation or
socket-side native access is added. No RMS, decay or loudness values are exported.
`post-messages` is disabled without a client; this suppresses bus traffic, but does
not promise to eliminate the level element's internal native analysis cost.

Before a measurement arrives, an endpoint is null. Non-finite channel values become
null; finite native silence readings are preserved. Values expire after the larger
of 250 ms or three requested intervals without a received measurement. Endpoint
windows are independent and not guaranteed to represent the same PCM interval
because DSP latency/queuing can separate them.

Disable, NULL recovery and teardown clear both caches. Native element identity
rejects messages from old generations; a GStreamer message sequence fence also
rejects queued pre-enable messages when native elements are reused. Endpoint
values join the existing worker-owned telemetry cache; transport is unchanged.

## Validation limits

Unit tests use fake native properties and clocks. Short synthetic Pi checks can
establish property presence/readability and that meters update without additional
visibility changes, but cannot prove plugin getter thread safety or sustained
50/100 Hz performance. `collect_duration_ns` times DSP property collection, excluding
socket I/O, cache copying and native `level` analysis/message handling. Target measurements with up to eight active DSP
Sources, especially on Pi Zero 2 W, remain necessary before claiming a performance
budget. No RF validation is implied.
