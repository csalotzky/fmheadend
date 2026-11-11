# Audio backend contract (Phase 1)

The opt-in Phase 2A HTTP process prototype and its asynchronous lifecycle/API
limitations are documented in [audio-worker.md](audio-worker.md). The default
in-process backend and the Phase 1 ownership rules below remain unchanged.

`daemon/sources/pipeline.py` is the control-side contract. `GstPipeline`
implements `MediaPipeline` directly and structurally: no wrapper, duplicated
status cache, native-object interface or new lifecycle executor is introduced.
`SourceManager` receives the existing `pipeline_factory`; `app.py` remains the
composition root selecting the concrete implementation. ModulatorManager,
status publication, RDS and mux still use SourceManager or its stable output.

## Ownership and observations

| Surface | Owner and semantics |
|---|---|
| `source` | Backend's applied source identity. Readers do not query SQLite. Radio recovery calls `update_source_identity` only after guarded persistence; this records identity without restarting/reconfiguring native playback. |
| `audio_profile_id` | Manager assigns the resolved profile identity before publishing the backend. The profile content is passed to the factory. |
| `start`, `check_startup` | Synchronous operations with existing exceptions. Construction/first-PCM readiness and LOCAL pending behavior remain distinct. `SourcePending` retains registered ownership and consumers. |
| `startup_pending`, `fatal_error` | Cached observations; no native state query or wait. The public pending property replaces manager access to `_startup_pending`. `check_startup` continues to own startup failure classification. |
| `stop` | Existing synchronous cleanup. Failure retains manager ownership; no replacement may start over a potentially live backend. No claim that a timeout terminates a native thread. |
| `output` | Stable `PcmOutput` fanout, shared by this source's TX consumers. Replacement receives exactly the same object; it must not silently allocate a new output. Attach/detach remain manager-controlled. |
| `snapshot`, `pcm_snapshot`, `probe_result` | Cached observations/counters, with the current schemas and exception behavior. No new polling of GStreamer from control/status readers. Snapshot returns a detached dictionary. |
| `request_telemetry`, `telemetry_snapshot` | Flag request and cached observation; request must remain safe for terminal shutdown outside the control queue. Native meter changes remain worker-owned. |
| `radio_url` | Optional `RadioRecovery` port: cached `observe`, and `offer/accept/discard` operations. The caller cannot depend on `HttpUrlRecovery` locks, retry internals or worker notifications. |

There is one backend per active **source ID**, not per TX. Manager registry
identity remains the ownership token. The factory receives existing profile,
output, reconnect, timeout, PCM limits, telemetry and podcast database-path
options. `validate_profile` remains on the factory. Its native work and the
podcast database-path dependency are deliberately unchanged in this phase.

## Mock and injection compatibility

`SourcePipeline` is the minimal mock lifecycle/identity contract;
`MediaPipeline` documents the complete production backend. Existing mock
factories still take only `source` and retain manager-generated MOCK status.
Manager `getattr`/`hasattr` capability fallbacks remain for lightweight injected
test doubles: absent pending/fatal state means false/none, absent telemetry is
unavailable, absent PCM counters are omitted, absent startup checking is a no-op,
and absent radio recovery disables that feature. `mocked` retains its existing
default-true compatibility behavior. New production backends should implement
the full contract rather than rely on these fallbacks.

Radio-capable test doubles must implement the explicit identity-update method.
The existing radio-recovery fake was updated without changing its assertions.
No production runtime `isinstance` gate is imposed on injected factories;
runtime protocol checks in focused tests verify the complete surface only.

## Behavior intentionally preserved

Acquisition publishes ownership before start, repeated acquisition checks pending
startup, last-consumer release stops before removal, failed stop retains ownership,
and source/profile replacement preserves output/consumer identity. Persistence
still precedes apply, and saved-but-not-applied errors retain their classification.
Radio recovery still confirms PCM before guarded URL persistence, then records
identity before accepting the trial. Token identity, cancellation, backoff and
retry behavior are unchanged. Native pipeline construction, sequencer callbacks,
DSP, PCM queues, RDS, mux and hardware code are untouched.

## Phase 2 boundary, not yet implemented

A process backend can reuse these cached observations, output facade, factory
injection and explicit radio-recovery port. It must supply a local facade for
the port; Python token/object identity is not a wire protocol. Snapshot payloads
are currently dictionaries with existing model validation, not versioned IPC.

Current synchronous start/stop/profile-validation/probe operations can occupy
the global control executor. A future supervisor requires separate intent and
completion, independent deadlines/reaping, worker generations and stale-event
rejection. Merely waiting synchronously for remote STOP would preserve the same
head-of-line blocking problem. Startup acceptance versus prepared versus PCM
ready, output-generation flushing, native probe isolation and shutdown budgets
must be redesigned then. Catalog/history ownership and recovery checkpoints also
remain future work. Phase 1 adds none of those mechanisms and does not fix native
deadlocks or change STOP/timeouts.
