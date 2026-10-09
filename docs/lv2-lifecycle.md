# LV2 profile-save crash investigation

## Result and limits (2026-10-10)

The original interactive daemon was already stopped by GDB (PID 55849, tracer
55559). It was not resumed, killed, detached, or replaced. Production SQLite,
profiles, RF, mux, priorities and telemetry socket were left alone.

Tests used the same application `PUT /api/v1/audio-profiles/files/custom/moderate_loud.json`
route, ControlExecutor, SourceManager, GstPipeline and real installed LV2 plugins.
Only storage, RP and mux were isolated/mocked. The exact production Moderate Loud
document was copied; only `stages.wideband.selected` alternated between `OFF` and
`LSP_COMPRESSOR`. All tuning values were retained. Each Source had its own real
HTTP MP3 pipeline (ORF FM4 Icecast URI), one consumer, with worker telemetry at
50 Hz. The telemetry exporter/socket client was not run in the isolated process;
the real worker's meter collection was enabled directly on its control executor.
This tests source count, not multiple TXs attached to one Source.

Installed target: Pi 3B+, aarch64, GStreamer 1.26.2, Lilv 0.24.26-1,
LSP LV2 1.2.21-5. Local and deployed base revision:
`435388c5bfca5d7a20ab3ba700b55baacda6a955`.

| Case | Active Sources | Sources sharing edited file | Baseline save iterations | Guarded save iterations |
| --- | ---: | ---: | ---: | ---: |
| A | 1 | 1 | 30 | 20 |
| B | 2 | 2 | 30 | 20 |
| C | 5 | 5 | 30 + 70 instrumented | 30 |
| D | 5 | 1 | 30 | 20 |

Both matrices completed without a native crash or failed application. The saved
profile initially had Wideband OFF: the first iteration of each original run was
an unchanged-selection save (which still rebuilds), followed by alternating
structural changes. Thus the baseline totals 190 saves / 185 structural switches;
the guarded matrix totals 90 saves / 86 structural switches. The final tool now
inverts the actual initial selection, checks playback after the final mutation,
and prints success only after application shutdown completes.

No new SIGSEGV occurred in this bounded matrix. That does not refute the supplied
crash reports. A temporary native interposer, loaded only into the isolated
baseline, recorded **54 lifecycle entries overlapping another entry** during C's
70 additional toggles. Both instantiate/instantiate and instantiate/free overlap
occurred. Three threads entered instantiate with the same LilvPlugin pointer at
monotonic times 961608.973742, 961608.973996 and 961608.974646. During profile
rebuilding, thread 57111 entered instantiate at 961613.087491 while thread 57131
entered free at 961613.087980 and again at 961613.091143.

This demonstrates unsafe native lifecycle concurrency, not a reproduced
use-after-free at the reported crash instruction. Neither the exact historic
Lilv failure nor the earlier AV1 assertion is conclusively attributed by this run.

## Code path and mechanism

`api.routes.save_profile_file` runs `SourceManager.save_profile` on the serialized
control executor. Validation creates temporary GstElements but does not drive
them to negotiated audio state. `_restart_audio` stops and joins the old worker,
then starts its replacement. The loop over Sources is sequential in Python.

However, `GstPipeline.start()` waits for `_prepared`, and `_attempt()` sets that
after requesting PLAYING. It deliberately does not wait for a live sender or
decoded PCM. Native caps negotiation continues on GStreamer streaming threads.
The next Source's shutdown or startup therefore overlaps the previous Source's
LV2 instantiation. Ordinary reconnects can do the same independently of Save.
There is no evidence that two application workers own the same Source.

GStreamer 1.26.2's gst-lv2 keeps one `gst_lv2_world_node`. Its
`gst_lv2_filter_setup` -> `gst_lv2_setup` calls `lilv_plugin_instantiate` during
caps negotiation; `gst_lv2_filter_stop` -> `gst_lv2_cleanup` calls
`lilv_instance_free`. These operations are not globally serialized by gst-lv2.
Lilv 0.24.26's `lilv_lib_open/close` mutate the shared `world->libs` tree and
non-atomic `LilvLib.refs`. Overlap can invalidate library-cache/descriptors while
another Source uses them. The GIL cannot protect native calls on these threads.

The Lilv "No plugin" message is emitted when descriptor enumeration returns
NULL before finding the requested URI; it is not a package-presence check.
A corrupt or prematurely released library record can therefore produce that
message even with a valid installed Autogain plugin. This is consistent with
the reported crash, but the exact corrupt pointer was not captured here.

Adding `get_state()` after NULL is not the demonstrated solution: NULL/READY
transitions cannot return ASYNC, and the old worker is joined before replacement.
Waiting indefinitely for first PCM would violate the existing missing-sender
startup contract, and a lock around `_build()` would end before LV2 setup runs.

Primary source references:

- [gst-lv2 global world](https://github.com/GStreamer/gstreamer/blob/1.26.2/subprojects/gst-plugins-bad/ext/lv2/gstlv2.c)
- [gst-lv2 setup/cleanup](https://github.com/GStreamer/gstreamer/blob/1.26.2/subprojects/gst-plugins-bad/ext/lv2/gstlv2utils.c)
- [Lilv library cache](https://github.com/lv2/lilv/blob/v0.24.26/src/lib.c)
- [Lilv descriptor enumeration](https://github.com/lv2/lilv/blob/v0.24.26/src/instance.c)
- [GStreamer state-change contract](https://gstreamer.freedesktop.org/documentation/gstreamer/gstelement.html#gst_element_set_state)

## Fix boundary and installation

`daemon/sources/lv2_lifecycle.c` exports the two Lilv lifecycle entry points and
uses one native mutex across calls to the real library. `load_gst()` installs the
library globally before GStreamer initialization/plugin loading. This protects
streaming-thread setup and teardown from any Source, including reconnects and
LOCAL fallback, without changing source readiness or adding an audio worker.

There is no lock around PCM processing, LV2 run, or meter collection, and no
profile/DSP tuning changes. The real Lilv library handle is kept alive for the
process lifetime. Missing binaries, an already loaded Lilv, or conflicting
interposition are explicit startup failures rather than an unsafe fallback.
This is a Linux ELF guard for the existing gst-lv2/Lilv boundary, not a general
claim that arbitrary plugin code or all Lilv APIs are thread-safe. In particular,
it does not wrap preset editing, which fmheadend does not use.

Linux package installation now compiles a small ctypes shared library through
setuptools. A C compiler is needed; no Python or Lilv development headers and no
additional runtime package are required. Wheels are platform-specific.

```sh
# From the repository root, when separately deploying the candidate:
~/venvs/fmheadend/bin/python -m pip install ./daemon
# For the project's editable deployment instead:
~/venvs/fmheadend/bin/python -m pip install -e ./daemon
```

A fresh daemon process is required. Updating a .py file in an already initialized
process cannot safely interpose existing native bindings. The investigation did
not deploy this candidate or restart the user's debugger-held process.

## Reproduction and evidence

After installing a candidate in an isolated environment:

```sh
gdb -q -batch \
  -ex 'set pagination off' -ex 'set print thread-events off' \
  -ex 'set debuginfod enabled off' -ex run -ex 'bt 20' \
  -ex 'thread apply all bt 20' \
  --args ~/venvs/fmheadend/bin/python -B daemon/tools/lv2_profile_repro.py \
  --profile /var/lib/fmheadend/audio-profiles/custom/moderate_loud.json \
  --uri https://orf-live.ors-shoutcast.at/fm4-q2a \
  --sources 5 --affected 5 --toggles 30 --telemetry > repro.log 2>&1
```

The tool uses TestClient to invoke the real API route with mock hardware. It
prints the exact mutation, iteration, active Source count and states before
each PUT. GDB captures signal/backtraces and stderr. A failure to reach RUNNING
or apply a save is a failed run, not a successful toggle. All documents and DB
changes are private temporary files, including when reproducing with the existing
production profile path as input. For A/B/C/D use `(sources, affected)` values
`(1,1)`, `(2,2)`, `(5,5)`, `(5,1)` in separate processes.

Pi evidence directory: `/tmp/fmheadend-lv2-audit.RWZn38/`. It contains A–D logs,
`C-trace.log`, the temporary hook source, candidate matrix logs, native test log,
and `bindings.log`. Loader diagnostics confirmed both calls in the actual
`libgstlv2.so` bind to `_lv2_lifecycle...so`; a two-buffer Autogain pipeline
completed and tore down successfully.

`test_lv2_lifecycle.py` tests guard load ordering and explicit failure, plus a
Linux C regression with a fake Lilv and an RTLD_LOCAL client. Eight native
callers perform 160 instantiate/free operations: the negative control overlaps,
the guarded case has zero overlap, and late installation is rejected.

## Final validation

- Full Windows backend: **1050 passed, 69 skipped** (native/platform-specific
  tests unavailable on Windows); existing Starlette dependency warning only.
- Pi targeted selection: initially **223 passed, 1 failed, 1 skipped**. Failure
  was an existing native pipeline-order assertion that omitted the two `level`
  meters, unrelated to lifecycle serialization. After correcting it, the entire
  GStreamer suite plus lifecycle tests passed: **34 passed, 1 skipped** (no ALSA
  capture device selected). All 224 runnable tests in the selection were thus
  validated. The corresponding stale mocked order assertion was also corrected.
- Guarded native matrix: 90 save iterations, no crash or failed application;
  final strengthened reproducer smoke: two actual switches, both Sources RUNNING,
  and successful cleanup.
- Native guard compiles with `-Wall -Wextra -Werror`; Linux aarch64 wheel build
  succeeds without Python/Lilv development headers. Actual gst-lv2 symbol binding
  and Autogain instantiate/process/free were verified independently.
- `git diff --check` passed. No production deployment, profile tuning change,
  service restart, scheduling change, or specification boundary change occurred.

The guard removes the demonstrated shared-cache concurrency. It is not proof that
all historical crashes had this cause. A crash at this same signature with the
guard installed would require a new captured native stack and library-binding
check; the AV1 assertion remains a separate unproven observation.
