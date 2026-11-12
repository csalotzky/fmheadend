# Experimental HTTP / LOCAL / PODCAST audio worker

## Phase 4: PODCAST integration

`--experimental-audio-worker` also routes PODCAST to audiod. The parent adapter
owns one PodcastFeedService, SQLite v1 and the shared RSS parser. Worker restart
and profile-only apply retain the service. Feed configuration changes and STOP
close it on a daemon cleanup thread, retaining pending state until both service
cleanup and child reap finish. A stalled fetch can outlive repeated 30-second
close attempts; abrupt parent death is outside the graceful cleanup guarantee.

The child injects WorkerPodcast into the existing PodcastRuntime, without starting
a feed service, database connection or parser process. SMART/RANDOM, tickets,
prefetch and the 60-second PCM threshold reuse the existing code. PODCAST PCM now
carries its rendering metadata through staging using the LOCAL capture mechanism.

Private v1 messages add `podcast_snapshot` and `podcast_played`. Catalog/history
JSON is ASCII-encoded into chunks with revision, exact offset, text and final flag.
Limits: 24,000 characters per chunk; 8 MiB per snapshot; 10,000 episodes and 10,000
history rows; 500-character titles; 8192-character IDs/media URLs; 48,000 encoded
characters per episode. Outer framing retains the 64 KiB JSON limit. Complete,
validated snapshots publish atomically. Existing source/generation/sequence checks
fence transport. Fresh workers request current durable history without refetching RSS.

The child queues at most 64 played notifications and sends each once. The parent
validates feed/episode data and queues the existing mark_played write. No played
ACK, retry, occurrence ID, deduplication or schema change exists. Crash/uncertain
send can lose an event. History snapshot merges retain optimistic maxima.

Empty-feed waiting uses the short lease renewed by the progressing native idle
loop. Active playback is still monitored during RSS refresh. Restart reconstructs
a schedule without position resume. Windows tests cover policy, persistence and
socket/lifecycle integration with simulated process handles. Raspberry Pi tests
on GStreamer 1.26.2 passed eight distinct native cases across focused runs:
MP3 multi-feed SMART/RANDOM playback, PCM metadata, limiter/clipper, the real
60-second history threshold, crash/native-callback-stall recovery with HTTP/LOCAL
siblings, profile apply and empty-feed refresh/source apply. These runs do not
qualify sample-exact gaplessness, physical RF continuity or sustained load.

## Phase 3: LOCAL playback

`--experimental-audio-worker` selects workers for HTTP and LOCAL. The existing
`--experimental-http-worker` retains HTTP-only routing; neither is enabled by
default. Both require Linux and reject `--mock`. ALSA/UDP/RTP are unchanged.
The Phase 2A text below records the original HTTP-only scope.

The parent resolves LOCAL directory/M3U catalogs with the existing resolvers and
global eight-slot scan pool. The child runs the existing `LocalPlayback`,
`PlaybackQueue` and `SequencedPlayback`: random/sequential selection, previous
cycle avoidance, remaining-unselected <=10 refresh trigger, prebuilt fallback,
prepared/submitted/rendering tickets, playbin3, about-to-finish, DSP and PCM.
There is no copied playlist algorithm, database in the child or nested process.

`local_request`/`local_response` extend the private v1 control channel. Requests
are generation-scoped, increasing integers. A scan request is its catalog
revision; only one scan and one cycle request can be outstanding per worker.
Catalog chunks contain request, exact offset, typed path/URL entries and final
flag. Completed snapshots publish atomically to the original LocalPlayback job;
its existing revision/generation logic applies the next queue at a cycle boundary.
Interrupted, repeated or out-of-order chunks cannot publish a partial catalog.
The immutable source configuration is held in the parent; child requests cannot
select another directory. Existing source/generation/channel-sequence checks run
before dispatch. No native callback waits for these messages.

Limits: 100,000 entries, 16 MiB cumulative UTF-8 locator bytes, 8192 characters
per locator and 56,000 JSON-escaped bytes per entry. A frame holds at most 64
entries and about 56 KiB of encoded entries, under the existing 64 KiB JSON cap.
Exceeding a limit reports `LOCAL_CATALOG_LIMIT`, without partial playback input.
Large directories still use the original resolver's transient full result in the
scan job; this is not a streaming filesystem enumerator.

Cycle computation stays inside the child but requires a lease in the parent's
existing eight-slot CPU pool, shared with in-process work. The lease is released
on completion or **after actual child reap**, never on a cleanup timeout.
Cancelled filesystem work retains its scan slot/source key until the OS call
returns, including across worker replacement. No waiter thread holds a lease.

LOCAL PCM carries its serialized sink metadata snapshot through native staging
before it is encoded for IPC. Prefetch and URI submission cannot relabel older
staged PCM. Only PCM records publish rendered identity to the parent, not control
status. First-block cached caps/probe fields may follow the first staging drain.
Existing per-TX/mux buffering remains; this is not sample-exact RF/RDS timing.

LOCAL start remains pending until valid PCM arrives. Worker `prepared` means
native startup was scheduled, not that discovery or decoding has completed.
The child control loop reports `local_waiting` only while the native idle loop
has refreshed a two-second expiry. Missing/unreadable/empty catalogs retry with
the existing five-second discovery delay; all-failed cycles are retired and
rediscovered rather than exhausting the process restart budget. This is an
explicit worker-only availability improvement; in-process error behavior is
unchanged. Parent status/exit/fatal deadlines always remain active. Expected
media waits suspend only the PCM deadline. Leaving the wait starts a bounded
PCM grace; a native state-transition stall cannot indefinitely claim discovery
progress. Last-rendered identity is retained until subsequent PCM or terminal
error, while waiting/error state remains visible.

Crash/stall recovery, asynchronous STOP/apply, shared output identity, generation
flushing, actual reap-before-replacement and restart budgets reuse Phase 2B.
Restart generates a fresh queue/cycle; neither position nor previous random-cycle
history is restored. A failed source can have an audible interruption. No RF
operation is introduced. No GStreamer patch is applied.

Native evidence and exact qualification limits: `tmp/audiod-phase3/RESULTS.md`.

## Phase 2B recovery

The daemon's existing control-owned maintenance tick services `WorkerRecovery`.
Only adapters exposing the worker recovery observation participate. No new
heartbeat, scheduler thread, RF command or mux replacement is involved.

`RecoveryPolicy` supplies injectable thresholds: 5 s without validated status,
15 s without valid PCM while running, or 90 s while BUFFERING/CONNECTING.
Times are parent monotonic observations; any valid PCM, including digital zero,
counts as progress. Status changes and reconnect counters do not reset the PCM
deadline. A responsive control thread therefore cannot hide a playback stall.
These are internal policy settings, not new CLI/WebUI options. Detection runs
on the existing maintenance cadence and can be delayed by unrelated control work.

Crash, transport error or timeout requests bounded teardown on the existing
owner. A replacement cannot start until `stop()` confirms actual reap. The
replacement has fresh channels/generation and the identical parent `PcmOutput`.
Automatic retries retain the applied source/profile snapshot. At most three
restarts are requested within 300 s, with 1/2/4 s backoff; exhaustion is terminal
ERROR until an explicit source/profile apply or stop/start. An explicit apply
resets that source's budget. There is no implicit restart on intentional STOP.

SourceManager retains pending cleanup, automatically completes removal after
last-consumer STOP, and coalesces source/profile apply to the latest requested
source. Existing save responses can still report `applied=false` with a pending
message because native completion is asynchronous; no manual retry is required.
Subsequent source status follows the new generation. Failed replacement remains
ERROR. All callbacks/cache publication retain the Phase 2A generation checks.

Native Pi validation and limitations are recorded in `tmp/audiod-phase2b/RESULTS.md`.

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
cannot launch an overlapping replacement. The Phase 2B manager maintenance path
automatically finishes pending STOP/apply; direct adapter callers still receive
the pending exception. All in-process lifecycle behavior stays
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
  there is no control-executor process wait. Manager maintenance handles recovery.
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
publishes an error. Unrelated adapters keep running. Phase 2B restarts the worker
within its budget, without an RF operation or mux reset. Tail audio already in muxd may
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
- Checkpoint/resume, admission limits, robust parent-death containment, richer
  error classification and production rollout.
- PCM blocks exceeding 64 KiB, source/profile JSON exceeding 64 KiB, and buffer
  settings above 10000 ms are outside the PoC wire limits and fail explicitly.

Linux qualification should run `python -m pytest daemon/tests/test_audio_worker.py
-q -ra --tb=short` from a checkout installed in that Linux virtual environment.
The suite includes genuine child processes, blocked native-lifecycle fixtures,
sharing, independent exit, parent EOF, and local HTTP WAV decoding with -6 dB
gain. Missing GI/plugins are skips, not native validation. Accessing a Mint VM
requires separate human authorization; these commands do not imply SSH approval.
