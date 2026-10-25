# fmheadend Copilot Instructions

## Canonical specification and scope

- Read [`docs/fmheadend-software-architecture.md`](../docs/fmheadend-software-architecture.md) before making architectural or externally observable behavior decisions. It is the canonical V1 contract; milestone documents record implemented details and validation procedures.
- This is an eight-channel FM headend for a Raspberry Pi Zero 2 W. Keep implementation choices suitable for that target, even when developing on a more capable host.
- Work one requested milestone at a time. Do not redesign the system or introduce Docker, Redis, message brokers, authentication, WebSockets, separate database servers, or speculative services unless explicitly requested.
- Firmware changes are only in scope when explicitly requested. Its Linux-facing contracts are defined by the architecture document and [`docs/rp2354-protocol.md`](../docs/rp2354-protocol.md).

## Build, test, and formatting

Run commands from the repository root unless noted. The Python daemon needs Python 3.11+; real GStreamer tests need the distro PyGObject/GStreamer packages and a virtual environment created with `--system-site-packages`.

| Area | Full check | Focused check |
|---|---|---|
| Python daemon | `python -m pytest daemon/tests -q -ra` | `python -m pytest daemon/tests/test_rds.py -q -ra` or `python -m pytest daemon/tests/test_gstreamer.py -k <expression> -q -ra` |
| C++ mux daemon | `cmake -S muxd -B build/muxd -DCMAKE_BUILD_TYPE=Release` then `cmake --build build/muxd -j2` and `ctest --test-dir build/muxd --output-on-failure` | `ctest --test-dir build/muxd -R '^ring$' --output-on-failure` |
| Angular Web UI (from `webui/`) | `npm ci`, `npm test`, `npm run typecheck`, `npm run format:check`, `npm run build` | `npm test -- --include src/app/features/sources/sources.spec.ts` |
| Controller native firmware tests (in the [Controller repository](https://github.com/csalotzky/fmheadend-controller)) | `cmake -S tests -B build/tests` then `ctest --test-dir build/tests --output-on-failure` | `ctest --test-dir build/tests -R '^test_rds_fifo$' --output-on-failure` |

- Python tests inject mock pipelines, mux clients, RP2354 clients, temporary SQLite databases, and clocks. Preserve this hardware-free testability. `gstreamer` and `muxd` pytest markers identify native-dependency tests.
- There is no configured Python linter. C++ warnings are enabled by CMake; Web UI formatting is checked with Prettier.
- On a Windows network share, run Web UI builds from a local copy of `webui/` because esbuild may be blocked. Keep source and lockfile changes in the repository.

## Architecture

The system has separate audio and control planes:

- `daemon/` packages `fmheadendd`, the Python FastAPI control daemon. It owns SQLite desired configuration, source/pipeline orchestration, RDS encoding/scheduling, RP2354 I2C control, runtime aggregation, and the REST API.
- `muxd/` builds the independent C++17 `fmheadend-muxd`. It accepts eight PCM Unix streams, keeps bounded per-channel rings, zero-fills underruns, discards oldest audio on overflow, builds fixed SPI frames, and sends them to the RP2354. It must not acquire GStreamer, SQLite, RDS, or QN configuration responsibilities.
- The independent [fmheadend-controller repository](https://github.com/csalotzky/fmheadend-controller) contains the RP2354 implementation. It owns SPI frame receipt, I2S/DMA output, QN8007B hardware operations, I2C protocol execution, and RDS FIFO transport.
- `webui/` is an Angular standalone application. `core/api/` owns explicit REST types/calls; feature components own UI state. It uses relative `/api` requests and the development proxy, never a hard-coded target hostname. Production assets are served by `fmheadendd --webui-dir`.

Digital audio follows one fixed downstream contract: 48 kHz, interleaved stereo, signed 16-bit little-endian PCM. GStreamer performs ingest, decoding, conversion, resampling, and optional processing; Python does not perform sample-by-sample DSP. The mux frame is a little-endian 32-bit counter followed by eight consecutive 512-byte channel blocks (4100 bytes total).

`fmheadendd` composes services in `daemon/app.py`: `Database`, `SourceManager`, `ModulatorManager`, RP and mux clients, `RadioTextService`, `RdsRefill`, `Recovery`, and `StatusService`. REST handlers are thin and run state-changing/control work through the single-worker `ControlExecutor`; media callbacks must not access SQLite or hardware directly. Runtime status is cached/observed state, not persistent configuration.

## Repository-specific conventions

- SQLite is the sole persistent configuration store. Its schema is currently version 1 and uses transactional initialization plus `PRAGMA user_version`; unsupported nonzero versions fail explicitly. Shared Audio Profiles are schema-1 JSON files, not SQLite entities.
- The eight physical modulators are permanent resources with IDs `0..7`, never CRUD-created or deleted. `InputSource` objects are reusable and demand-driven: one active digital source pipeline fans out to all assigned modulators, and the final consumer releases it.
- A modulator's persisted `enabled` field is desired state, distinct from observed RF state. Use explicit start/stop lifecycle operations rather than patching it. Configuration edits require confirmed OFF and released source ownership; use the dedicated save-and-resume lifecycle path for an operational transmitter.
- `ANALOG` is a modulator-owned hardware jack mode, not an `InputSource`: it clears `input_source_id`, creates no GStreamer pipeline or PCM producer, and keeps the mux lane silent. `DIGITAL` needs a configured source before start.
- Preserve bounded buffering and low-latency behavior throughout: missing PCM becomes silence; old audio is dropped on overrun; no unbounded queueing or replay of stale audio. The source output and mux rings are intentionally bounded independently.
- Keep RP2354/QN8007B access behind the RP client/service abstraction, and mux socket access behind `UnixMuxClient`. REST routes must not directly manipulate GStreamer, I2C, SPI, or database internals.
- RDS content selection, encoding, and refill policy run on Linux/Python. The RP interface is a credit/free-space-controlled FIFO transport; do not move scheduler policy into firmware or fabricate on-air state in the API/UI.
- API errors use `{"error": "CODE", "message": "Explanation"}`. Report dependency, validation, and partial-apply failures explicitly; do not silently replace unavailable hardware or processing with a successful fallback.
- `create_app()` deliberately defaults to mocks for tests and embedding. The CLI defaults to real GStreamer, Unix mux, and I2C; use `--mock` for all legacy mocks, `--mock-rp` for real ingest/mux with mock RP control, or `--mock-rp --mock-mux` for standalone operation.
- The frontend distinguishes configured/desired values from observed runtime state. Its polling is owned by the shell, does not overlap requests, and retains stale values as explicitly stale/unknown after failures.
