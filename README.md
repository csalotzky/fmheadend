# fmheadend

Eight-channel FM headend for Raspberry Pi Zero 2 W. The canonical design is
[docs/fmheadend-software-architecture.md](docs/fmheadend-software-architecture.md).

Milestones 1-3 provide the Python control backend, real GStreamer ingest, and
the independent C++ PCM mux daemon with mock and Linux spidev transports.
Configuration remains in SQLite; RP2354 control remains mocked.
See [Milestone 3 build, run, wire format and verification](docs/milestone3.md).
See [Milestone 2 setup and verification](docs/milestone2.md) for transport details,
dependency installation, integration tests and smoke tests.

## Run on Linux Mint / Raspberry Pi

Python 3.11 or newer is required. On Mint 22.x, install the native runtime:

```sh
sudo apt update
sudo apt install python3-venv python3-gi python3-gst-1.0 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-alsa alsa-utils
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -e './daemon[test]'
fmheadendd --mock-mux --database ./fmheadend.db --host 127.0.0.1 --port 8080
```

The system-site-packages flag exposes distro PyGObject/GStreamer bindings to the
virtual environment. Use the distro Python interpreter; a separately installed
Python must have matching GI bindings. Older Mint releases whose default Python
is below 3.11 do not satisfy the existing package requirement.

The CLI uses GStreamer and the independent Unix mux daemon by default. The
command above uses `--mock-mux` for standalone Milestone 2 operation. Add `--mock`
for original Milestone 1 source and mux behavior without GI. RP2354 control is
mocked in all modes. The `create_app()` factory retains its mock defaults for
existing callers; inject `pipeline_factory=GstPipeline` and a `mux_factory`
returning `UnixMuxClient(sources.output_for)` for real ingest and mux integration.

Open http://127.0.0.1:8080/docs for Swagger or `/openapi.json` for the schema.
All REST routes use `/api/v1`. `python -m fmheadendd.app` accepts the same flags.
The default database is `/var/lib/fmheadend/fmheadend.db`; use a writable path
for local development. Run one daemon process/worker per headend.

The distribution maps `fmheadendd` directly to `daemon/` without an extra nested
source directory. `muxd/` contains the C++ daemon; `systemd/` and `webui/` remain reserved for later work.

## Try the API

Create a source, assign it, and start a transmitter with mocked RF control (replace the example URL with a real stream):

```sh
curl -X POST http://127.0.0.1:8080/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example radio","type":"HTTP","config":{"uri":"https://example.org/live.m3u8"}}'

# Substitute the returned source ID if it is not 1.
curl -X PATCH http://127.0.0.1:8080/api/v1/modulators/0 \
  -H 'Content-Type: application/json' \
  -d '{"input_source_id":1,"frequency_khz":98100,"rds_rt":"Fallback text"}'

curl -X POST http://127.0.0.1:8080/api/v1/modulators/0/start
curl http://127.0.0.1:8080/api/v1/status

curl -X PUT http://127.0.0.1:8080/api/v1/modulators/0/rds/rt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Temporary programme announcement","ttl_s":10}'
curl http://127.0.0.1:8080/api/v1/modulators/0/rds/rt
curl -X POST http://127.0.0.1:8080/api/v1/modulators/0/stop
```

Test an unsaved source with `POST /api/v1/sources/test` and the same body as
source creation. Test a saved source with `POST /api/v1/sources/{id}/test`.
Neither operation changes configuration, assignments, or active pipelines.
In real mode, probes wait for decoded audio and report success/failure, input
sample rate/channels, codec when available, negotiated output caps and a useful
message. Failed connections return `ok: false`, `mocked: false`; invalid request
models retain HTTP 422. Active saved-source tests reuse the running pipeline.
Analog tests bypass GStreamer and do not claim to verify physical audio.

Other endpoints cover `/audio-profiles`, `/system/config`, `/system/info`,
`/sources/{id}/status`, `/modulators/start-all` and `/modulators/stop-all`.
Errors use `{"error":"CODE","message":"Explanation"}`, including HTTP validation,
missing routes, resource conflicts and unavailable dependencies.

## Configuration and lifecycle semantics

- SQLite uses transactional schema initialization and `PRAGMA user_version`.
  A newer unsupported schema is rejected. Foreign keys prevent deleting assigned
  sources or referenced audio profiles; these deletions return HTTP 409.
- PATCH replaces supplied fields, including entire JSON configuration objects.
  Omitted fields remain unchanged. Explicit null clears nullable assignments;
  null for a required field is invalid. The merged configuration is revalidated.
- Start/stop persist `Modulator.enabled` as the desired state. An active modulator
  configuration update commits first, then turns RF off and reapplies it. Failed
  hardware application returns 503 and leaves desired configuration intact while
  actual state becomes ERROR. Shutdown turns RF off without changing desired state.
- Newly created sources default to enabled. Modulators default to disabled,
  unassigned, with `frequency_khz: null`, RF level 0, stereo, 50 us pre-emphasis
  and RDS disabled. A frequency must be explicitly configured before enabling
  a modulator; starting without it returns 409 `FREQUENCY_REQUIRED` without
  changing desired state or touching hardware. Starting without an enabled
  source also returns 409. A disabled modulator's frequency can be cleared with
  `PATCH {"frequency_khz": null}`. Start-all attempts every ID and
  returns per-ID statuses/errors; unassigned modulators do not silently succeed.
- One pipeline serves all consumers of a digital source. The last consumer
  stopping releases it. Analog sources create no pipeline or PCM producer.
  Source edits while active return 409; stop consumers before editing. Hardware
  stop failures retain the source until RF-off can be confirmed.
- The default FM validation band is 87500..108000 kHz inclusive. Override it with
  `--fm-band-min-khz` and `--fm-band-max-khz`. No tuning raster is assumed.
  `rf_level` is an opaque nonnegative integer pending the hardware contract;
  reported `rf_output_dbuv` is null, with no invented conversion or measurement.
- PI is represented as a uint16 integer. PS entries contain up to eight printable
  characters and a positive duration; RT allows up to 64 printable characters.
  Actual character encoding and RDS group generation are deferred to Milestone 5.
- RT priority is override, then enabled stream metadata, then persistent fallback.
  TTL uses monotonic time and expires automatically. Without TTL it lasts until
  deletion or restart; an empty override is valid. The additional
  `GET /modulators/{id}/rds/rt` endpoint exposes text selection for mock testing.
  Current RT is deliberately absent from dashboard status.
- Status requests read cached dependency status. A background task refreshes it
  according to the configured polling interval. CPU, memory, temperature and RF
  readback are null when unavailable. Mock counters do not simulate PCM traffic.
- System configuration is persistent. Listener/log settings take effect at the
  next CLI startup; host/port flags override saved values for that invocation.
  No real hardware transport reads these settings in Milestone 1.

## Tests

```sh
python -m pytest daemon/tests -q
```

Tests use temporary SQLite databases, injected mock clients/pipelines and a fake
monotonic clock for TTL boundary checks. They cover API validation and errors,
CRUD, foreign keys, migration/reopen, restart persistence, source test isolation,
shared pipelines, analog bypass, RF/audio ordering, failure cleanup, desired vs
actual state, concurrent requests, and RadioText priority/expiry.

## Milestone boundary and known limitations

Milestone 2 adds HTTP/HLS, raw UDP, RTP and ALSA ingest, appsink output, GStreamer
gain, metadata and source reconnects. See [details and test results](docs/milestone2.md).
Milestone 3 adds the Unix PCM/control sockets, bounded C++ rings, fixed frame
builder, mock capture and Linux spidev transport. No RP2354 I2C transport, RDS
encoder/FIFO scheduler, web UI, systemd integration or automatic desired-state
restoration is implemented. New modulators
remain disabled with no frequency until explicitly configured.

No architectural deviation is introduced. There is one source worker per active
digital source and a single serialized control executor inside `fmheadendd`;
media callbacks never access SQLite or hardware. This is not a separate service.
