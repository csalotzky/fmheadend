# Experimental HTTP audio worker (Phase 2A)

**Phase 2A final gate: PASS.** The same owner now
survives the cleanup deadline until a successful wait. All three earlier review
corrections are retained. Fresh Mint validation: 68 focused tests, zero skips;
ten additional runs of 14 ownership tests passed. See
`tmp/audiod-phase2a/FINAL-REVIEW.md` for evidence and shutdown guarantee limits.

**Native HTTP/process validation passed on Mint (initial run: 35 tests).
Full custom DSP-plugin qualification remains provisional:** FMheadend and LSP
plugins were unavailable there. See `tmp/audiod-phase2a/MINT-VALIDATION.md` for
scope, baseline failures and repeated lifecycle checks. This is an opt-in proof of concept, not a production
supervisor. The default `fmheadendd` CLI still selects `GstPipeline`; application
test construction still defaults to `MockPipeline`.

On Linux, `--experimental-http-worker` selects `ExperimentalHttpFactory`.
Only HTTP sources use `ProcessPipeline`; LOCAL/PODCAST/ALSA/UDP/RTP retain the
existing implementation. The switch is CLI-only, incompatible with `--mock`,
and does not alter installation or service defaults.

## Ownership

`SourceManager` still creates one backend per source ID. Its registry and
consumer sets provide sharing. `ProcessPipeline` retains the supplied
`PcmOutput` by identity and feeds its ordinary per-TX queues. No TX, RDS, mux,
database, API or discovery objects cross into the child.

Each adapter starts a fresh Python executable using `Popen`, `close_fds=True`,
two explicit `pass_fds`, and no `preexec_fn`. It executes
`python -m fmheadendd.sources.audiod`; the package also declares the equivalent
`fmheadend-audiod` console entry point. The child constructs the existing
`GstPipeline` from source/profile snapshots. Native decoding, source gain/DSP,
appsink pacing, staging and ingest reconnect policy are reused unchanged.

There are two parent threads per active worker: lifecycle/control IO and PCM
receive. The child has a control loop, native lifecycle thread, PCM sender,
and the existing GstPipeline/native threads. The native lifecycle thread is
separate from IPC ingress. No additional subprocesses are created by audiod.

## Wire contract

Two private Unix stream socketpairs separate control/events from PCM. Socket
buffers are requested at 32 KiB (the OS may adjust/double that setting). IO
uses finite 200 ms socket timeouts exclusively on IO/owner threads. `sendall`
handles short writes; an incomplete record timeout invalidates the connection.
No partial-frame tail is ever reused as a new frame.

Each frame is a network-order uint32 body length, uint32 JSON-header length,
UTF-8 JSON envelope, and optional raw PCM. Limits: 64 KiB JSON, 64 KiB PCM,
131076-byte body. Envelopes contain version 1, nonnegative source ID (0 for
unsaved probes), 32-character generation, positive per-channel sequence, kind,
and payload. Old generation/source/sequence records are ignored before cache
or output publication. Invalid schemas, format, framing or limits close the
generation. Configuration is immutable within a generation.

| Message | Semantics |
|---|---|
| `create` (request 1) | Source/profile/options snapshot; also requests start |
| `accepted` | Receipt of request 1 or 2; not native completion |
| `prepared` | Existing GstPipeline.start completed; normal HTTP need not yet have PCM |
| `status` | Cached native state, buffering/error, metadata and staging counters, approximately 5 Hz |
| `shutdown` (request 2) | Terminal shutdown intent; no subsequent playback command |
| `terminated` | Native lifecycle returned; parent still confirms actual process exit |
| `pcm` | Fixed-format bytes, production monotonic timestamp, matching metadata/status/probe snapshot |

PCM is post-DSP S16LE/48000/stereo/interleaved, frame-aligned in groups of four
bytes. No format negotiation or alternative format is supported. The explicit
wire format is independent of GStreamer's caps-string formatting. Native sample
validation remains in GstPipeline. There is no PTS clock or cross-source sync:
the existing appsink clock still paces production.

The worker output sink captures a metadata snapshot when GstPipeline publishes
a block and enqueues an encoded packet. It never performs socket IO there;
native appsink callbacks retain the original staging path. At most four queued
packets plus one sender packet are held; capacity drops the oldest, and queued
packets older than 100 ms are discarded. Worst-case encoded queue bound is
4 * 131080 bytes, independent of TX count. Parent rejects PCM older than 100 ms
using the same host monotonic clock; consumer queues retain their existing
capacity/age behavior. Stalled sends fail this worker, with no reconnect or
replacement of its IPC session in this phase.

Metadata in control events does not advance displayed title/artist/codec. Those
fields advance with their PCM record, so two-channel arrival order cannot show
future metadata against an older block. This preserves HTTP's existing cached
TAG semantics, not sample-exact station/tag boundaries: the current decoder
does not expose such HTTP boundaries. LOCAL/PODCAST rendered-item semantics
are outside this PoC. Existing mux and consumer buffers can still contain tail
audio; this is not a sample-exact RDS or RF delivery guarantee.

## Asynchronous lifecycle and compatibility limits

`start()` schedules launch on the owner thread and raises `SourcePending` until
prepared. Existing SourceManager ownership retention and TX audio-pending
retry handle this. The global control executor waits for neither process launch
nor a native completion. Startup has a parent deadline of native timeout + 1 s.

`stop()` sets terminal intent, clears the parent output and returns immediately
with `DependencyUnavailable` while reaping is pending. A later `stop()` succeeds
only after actual exit. The existing manager therefore retains ownership and
cannot launch an overlapping replacement. No new completion dispatcher was
added: **a STOP may report cleanup pending and require retry; saved source/profile
application may report saved-but-not-applied and require reapplication after
reap**. This deliberate experimental API limitation avoids disguising a blocking
RPC as the existing synchronous method. All in-process lifecycle behavior stays
unchanged. Normal manager close initiates all stops. A small, strong-reference
set retains active daemon owner threads' adapters independently of the manager
registry; an entry is removed only after successful reaping and cleanup.

Owner threads allow 300 ms graceful exit, then TERM/200 ms, then KILL/500 ms;
socket/reader timeouts add bounded overhead. They use owned Popen handles, not
delayed PID-only timers. Child EOF exits via `os._exit`, without Python/native
destructors. A surviving KILL deadline leaves an explicit error, `_done=False`,
and the **same** owner performing 200 ms bounded waits until exit is reaped.
There is no thread/handle handoff or second reaper to lose an exit race. Socket
resources are closed before this late loop; wait/signal exceptions retain the
owner and retry. A failed KILL is retried; successful KILL is never treated as
proof of exit. Independent source owners continue normally. No replacement is
permitted while cleanup is pending, and stop intent is never reset.

The guarantee has three distinct boundaries:

- **While the daemon's Python runtime remains operational:** an owner and its
  Popen handle remain reachable until wait succeeds, even if SourceManager.close
  has cleared its registry. Later exit is reaped without another control call.
  The total late wait is intentionally unlimited, but only on a daemon thread;
  there is no control-executor wait or automatic restart/completion dispatcher.
- **Graceful shutdown:** the application first stops submitting source work.
  An atexit hook closes admission, requests all owners to stop, and waits at most
  one shared 1.5 s budget, then attempts KILL on any still-pending handles.
  It never marks them complete. Owners continue waiting while Python runs; they
  cannot indefinitely hold interpreter exit. This is a bounded grace, not a
  promise that every kernel-stalled child has exited before the parent does.
- **After parent death/interpreter finalization:** Python threads cannot promise
  further waitpid calls. Linux reparents remaining children to init/a subreaper;
  eventual reaping then depends on that OS owner. A stuck launch that has not
  returned a Popen handle within shutdown grace, abrupt death, or a child unable
  to process EOF needs stronger external containment. No hard deadline for
  kernel-uninterruptible exit or full orphan containment is claimed here.

The strong owner set plus bounded atexit grace address the daemon-thread lifetime
explicitly; a daemon thread alone is not a guarantee after interpreter death.
No process group/cgroup service integration or Phase 2B supervisor is introduced.

Inactive HTTP probes use the same worker with `reconnect=False`. Their existing
discovery executor may wait for prepared **and received, valid PCM**, at most
native timeout + 1 s, and at most 1.8 s for cleanup. Active playback start/stop
never use those waits on the global control executor.

Worker crash/transport failure clears only its parent queues and metadata and
publishes an error. Unrelated adapters keep running. No worker restart, RF
operation, failover, or mux reset is performed. Tail audio already in muxd may
play before its existing silence fill takes over.

## Deliberately deferred

- Radio Browser candidate recovery/persistence IPC (`radio_url=None`). Native
  HTTP reconnect to the configured URL remains; this is a functional limitation
  of the experimental backend, not a change to the default backend.
- Native telemetry subscription/transfer: explicitly unavailable. Source status,
  native staging counters and parent consumer counters are cached; IPC drop
  counters are internal diagnostics, not added to the public PCM status schema.
- Native profile validation still uses the existing in-process validator, and
  other source types remain in-process. This is not total native crash isolation.
- Automatic STOP/apply completion integration, crash budgets, heartbeat/no-PCM
  classification, replacement recovery, checkpoint/resume, admission limits,
  robust parent-death containment, richer error classification and rollout.
- PCM blocks exceeding 64 KiB, source/profile JSON exceeding 64 KiB, and buffer
  settings above 10000 ms are outside the PoC wire limits and fail explicitly.

Linux qualification should run `python -m pytest daemon/tests/test_audio_worker.py
-q -ra --tb=short` from a checkout installed in that Linux virtual environment.
The suite includes genuine child processes, blocked native-lifecycle fixtures,
sharing, independent exit, parent EOF, and local HTTP WAV decoding with -6 dB
gain. Missing GI/plugins are skips, not native validation. Accessing a Mint VM
requires separate human authorization; these commands do not imply SSH approval.
