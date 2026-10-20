# fmheadend

Eight-channel FM headend for Raspberry Pi Zero 2 W. The canonical design is
[docs/fmheadend-software-architecture.md](docs/fmheadend-software-architecture.md).

Milestones 1-5 provide the Python control backend, real GStreamer ingest, and
the independent C++ PCM mux daemon with mock and Linux spidev transports.
Source/TX configuration remains in SQLite; shared Audio Profiles use JSON files; Milestone 4 adds real Linux RP2354 I2C control.
See [Milestone 4 setup, tests and hardware smoke procedure](docs/milestone4.md)
and the [exact V1 RP protocol](docs/rp2354-protocol.md).
[Milestone 5](docs/milestone5.md) adds Linux RDS 0A/1A/2A, ECC, fixed AF Method A,
PS/RT scheduling and acknowledged FIFO refill.
See [Milestone 3 build, run, wire format and verification](docs/milestone3.md).
See [Milestone 2 setup and verification](docs/milestone2.md) for transport details,
dependency installation, integration tests and smoke tests.

## Optional HTTP password protection

Authentication is disabled unless `--passwd-file [PATH]` is supplied. With no
path it reads `/var/lib/fmheadend/htpasswd`, directly above `audio-profiles/`.
Changing `--audio-profiles-custom-dir` does not change this location. An explicit
path selects a different file. Provide an Apache-compatible bcrypt htpasswd file
(for example, created interactively with `htpasswd -B`), readable only by the
service account and trusted administrators. The daemon never creates credentials.

The complete HTTP app is protected, including API, docs, static assets served via
`--webui-dir` (such as `www/`) and SPA routes. Browsers use their native Basic Auth
prompt. **Use HTTPS or a trusted encrypted transport: Basic Auth over plain HTTP
does not protect passwords against interception.** Do not place the password file
inside the served WebUI directory.

Missing, unreadable, empty or invalid password files prevent startup. At runtime,
file changes invalidate cached authentication; unavailable/invalid files deny
access until corrected. Only bcrypt `$2a$`, `$2b$`, `$2y$` entries are accepted,
with costs 4–16, UTF-8 usernames up to 128 bytes, passwords up to 72 UTF-8 bytes
and a 64 KiB file limit. Duplicate usernames are rejected. No legacy hash fallback.
Verification uses one worker independent of control/audio, at most eight pending
checks, and a 128-entry, 30-second cache of keyed credential digests. Excess requests
receive 401; no passwords or Authorization headers are cached or logged.

## Run on Linux Mint / Raspberry Pi

Python 3.11 or newer is required.

Linux installation also builds a small C library that serializes LV2 lifecycle
operations across GStreamer threads. A C compiler is required; see
[LV2 lifecycle safety and native-crash investigation](docs/lv2-lifecycle.md).

On Mint 22.x, install the native runtime and build tools:

```sh
sudo apt update
sudo apt install build-essential python3-venv python3-gi python3-gst-1.0 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-alsa alsa-utils
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -e './daemon[test]'
fmheadendd --mock-controller --mock-mux --database ./fmheadend.db --host 127.0.0.1 --port 8080
```

The application version comes from `daemon/pyproject.toml` through installed
package metadata. After changing it, repeat the editable install above and restart
the backend to refresh the version; regular installations get it from the package.

The system-site-packages flag exposes distro PyGObject/GStreamer bindings to the
virtual environment. Use the distro Python interpreter; a separately installed
Python must have matching GI bindings. Older Mint releases whose default Python
is below 3.11 do not satisfy the existing package requirement.

The CLI uses real GStreamer, the independent Unix mux daemon and Linux RP2354
I2C by default. The command above explicitly mocks RP and mux for standalone
Milestone 2 operation. Use `--mock-controller` with native mux for Milestone 3 operation
without an RP. Use `--mock` for all original Milestone 1 mocks without GI.
Real I2C requires an explicitly configured slave address; failure never falls
back to mock, and the HTTP server remains available with degraded status. The `create_app()` factory retains its mock defaults for
existing callers; inject `pipeline_factory=GstPipeline` and a `mux_factory`
returning `UnixMuxClient(sources.output_for)` for real ingest and mux integration.
Inject `rp=Rp2354Client(device, address)` from `rp2354.linux_client` for real control.

Open http://127.0.0.1:8080/docs for Swagger or `/openapi.json` for the schema.
All REST routes use `/api/v1`. `python -m fmheadendd.app` accepts the same flags.
The default database is `/var/lib/fmheadend/fmheadend.db`; use a writable path
for local development. Run one daemon process/worker per headend.

The LOCAL source path picker exposes only `/mnt`, `/media`, and `/srv` by default.
Add an extra absolute host path at startup with repeatable
`--media-browser-root`, for example `--media-browser-root /home/tzky --media-browser-root /home/tzky/Music`.
This is a read-only convenience browser: it neither mounts storage nor changes
the existing persisted LOCAL path semantics.

The distribution maps `fmheadendd` directly to `daemon/` without an extra nested
source directory. `muxd/` contains the C++ daemon; `systemd/` contains M6A service units; `webui/` contains the V1 Dashboard, Sources, Modulators, Audio Profiles and read-only System UI.

The [file Audio Profile system](docs/audio-profile-files.md) integrates shared JSON
profiles with Source selection, the WebUI and modular native/LSP DSP. See
[implemented mappings, dependencies and limits](docs/modular-dsp-implementation.md). Install its read-only JSON defaults separately as documented there, or use
`--audio-profiles-default-dir ./audio-profiles/default` in development. Profiles are files; SQLite and modular JSON use their current structures as schema 1. See that document
before upgrading populated databases.

## Try the API

Create a source, assign it, and start a transmitter with mocked RF control (replace the example URL with a real stream):

```sh
curl -X POST http://127.0.0.1:8080/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example radio","type":"HTTP","config":{"uri":"https://example.org/live.m3u8"}}'

# Substitute the returned source ID if it is not 1.
curl -X PATCH http://127.0.0.1:8080/api/v1/modulators/0 \
  -H 'Content-Type: application/json' \
  -d '{"input_source_id":1,"frequency_khz":98100,"rds_pi":4660,"rds_ecc":224,"rds_af_khz":[101100],"rds_rt":"Fallback text"}'

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
HTTP tests additionally report reliable ICY station identity and approximate input bitrate when available. The UI can fill a blank Source Name without saving. Analog input is a Modulator audio mode, not a Source and has no source-test operation.

Other endpoints cover `/audio-profiles`, `/system/config`, `/system/info`,
`/sources/{id}/status`, `/modulators/start-all` and `/modulators/stop-all`.
Errors use `{"error":"CODE","message":"Explanation"}`, including HTTP validation,
missing routes, resource conflicts and unavailable dependencies.

## Configuration and lifecycle semantics

- SQLite uses transactional schema initialization and `PRAGMA user_version`.
  Current pre-V1 schema is version 1; any other nonzero version is rejected with instructions to recreate the database. Foreign keys prevent deleting assigned
  sources or referenced audio profiles; these deletions return HTTP 409.
- PATCH replaces supplied fields, including entire JSON configuration objects.
  Omitted fields remain unchanged. Explicit null clears nullable assignments;
  null for a required field is invalid. The merged configuration is revalidated.
- Start/stop persist `Modulator.enabled` as the desired state. An active modulator
  configuration PATCH returns 409 without persistence or hardware changes. Explicitly
  stop, edit, then start. PATCH cannot change desired enabled state; use Start/Stop.
  Shutdown turns RF off without changing desired state.
- Sources run on demand from active DIGITAL modulators. Modulators default to disabled,
  DIGITAL and unassigned, with `frequency_khz: null`, unset relative RF output, stereo, 50 us pre-emphasis
  and RDS disabled. A frequency must be explicitly configured before enabling
  a modulator; starting without it returns 409 `FREQUENCY_REQUIRED` without
  changing desired state or touching hardware. Starting DIGITAL mode without a configured
  source also returns 409. ANALOG mode uses the channel's own jack, clears input_source_id, and requires no Source or Audio Profile. A disabled modulator's frequency can be cleared with
  `PATCH {"frequency_khz": null}`. Start-all attempts every ID and
  returns per-ID statuses/errors; unassigned modulators do not silently succeed.
- One pipeline serves all consumers of a digital source. The last consumer
  stopping releases it. ANALOG modulators create no pipeline or PCM producer.
  Source edits while active return 409; stop consumers before editing. Hardware
  stop failures retain the source until RF-off can be confirmed.
- Startup `--region` selects the UI frequency plan (default 1, Europe). Hardware
  validation is independently 76000..108000 kHz on a 50 kHz raster. See
  [M7B5](docs/milestone7b5.md) for all four regions and production static hosting.
  Configure `rf_attenuation_db` as relative attenuation: 0 (Maximum), then 1.5 dB
  steps through 22.5 dB. Controller firmware maps these levels without changing IPOW.
  Fresh Modulators have null attenuation. Select a level explicitly before real hardware use.
  See the [canonical protocol](docs/rp2354-protocol.md).
- PI is represented as a uint16 integer. PS entries contain up to eight printable
  characters and a positive duration; RT allows up to 64 printable characters.
  ECC is a nullable integer byte, with no inferred country. Fresh defaults are  ECC=null and AF=[]; see M5 for deterministic character conversion and scheduling.
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
CRUD, foreign keys, atomic initialization, unchanged reopen, unsupported-version rejection, restart persistence, source test isolation,
shared pipelines, analog bypass, RF/audio ordering, failure cleanup, desired vs
actual state, concurrent requests, and RadioText priority/expiry.

## Milestone boundary and known limitations

Milestone 2 adds HTTP/HLS, raw UDP, RTP and ALSA ingest, appsink output, GStreamer
gain, metadata and source reconnects. See [details and test results](docs/milestone2.md).
Milestone 3 adds the Unix PCM/control sockets, bounded C++ rings, fixed frame
builder, mock capture and Linux spidev transport. Milestone 4 adds RP2354 I2C protocol/client primitives and hardware status.
M5 adds Linux RDS encoding and FIFO scheduling. M6A adds safe desired-state
restoration, integration recovery and systemd deployment. M7A adds the Angular Dashboard; M7B1 adds Sources management; M7B2 adds Modulator/RDS configuration; M7B3 adds
[Audio Profiles management](docs/milestone7b3.md). M7B5 completes System diagnostics and production WebUI hosting. Controller firmware
remains unimplemented; Zero 2 W stress/performance validation is deferred to M6B. New modulators
remain disabled with no frequency until explicitly configured.

No architectural deviation is introduced. There is one source worker per active
digital source and a single serialized control executor inside `fmheadendd`;
media callbacks never access SQLite or hardware. This is not a separate service.

Milestone 6A lifecycle recovery and systemd deployment: [documentation](docs/milestone6a.md).

Milestone 7A Angular Dashboard: [development and validation](docs/milestone7a.md).


Milestone 7B1 Sources UI and final validation: [documentation](docs/milestone7b1.md).
Current SQLite schema is **1**. Fresh databases directly seed Modulators 0..7 and
SystemConfig; Sources start empty and Audio Profiles are shared modular JSON files
with `schema_version: 1`. There is no development-format migration or compatibility
path. Unsupported versions/structures fail explicitly. See
[file profiles](docs/audio-profile-files.md) and architecture section 16.4.

Modulator RF/Audio/RDS editor: [Milestone 7B2](docs/milestone7b2.md).

Production WebUI: build with `cd webui && npm ci && npm run build`, then pass the
browser asset directory using `fmheadendd --region 1 --host 0.0.0.0 --port 8080
--webui-dir /opt/fmheadend/webui` (one command, plus normal hardware/database flags).
Node is not required at runtime. See [deployment details](docs/milestone7b5.md).
