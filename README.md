# fmheadend

Eight-channel FM headend for Raspberry Pi Zero 2 W. The canonical design is
[docs/fmheadend-software-architecture.md](docs/fmheadend-software-architecture.md).

Milestone 1 provides the `fmheadendd` Python package and a hardware-free FastAPI
backend. Sources and audio profiles have CRUD APIs; modulators are fixed IDs 0..7.
SQLite stores configuration, while source/TX status and RadioText overrides live
only in memory. RP2354, mux and source pipelines are mocks.

## Run on Linux / Raspberry Pi

Python 3.11 or newer is required. No GStreamer, SPI, I2C or RF hardware is needed.
From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e './daemon[test]'
fmheadendd --mock --database ./fmheadend.db --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080/docs for interactive Swagger, or
http://127.0.0.1:8080/openapi.json for the schema. All REST routes use `/api/v1`.
`python -m fmheadendd.app` accepts the same arguments as `fmheadendd`.
Without a database override, the path is `/var/lib/fmheadend/fmheadend.db`; its
parent must be writable by the service account. Use one daemon process/worker
per database and headend because runtime state is process-local.

The distribution maps the `fmheadendd` import package directly to `daemon/`.
There is no extra nested source directory. `muxd/`, `systemd/` and `webui/` are
reserved for later milestones.

## Try the API

Create a source, assign it, and start a mock transmitter:

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
Results explicitly include `mocked: true`: success means valid mock configuration,
not verified connectivity or decodability. Analog probes report no digital format.

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
- One mock pipeline serves all consumers of a digital source. The last consumer
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

Only Milestone 1 is implemented. There is no real GStreamer ingest, audio DSP,
PCM transport, C++ mux implementation, SPI/I2C access, RP wire protocol, raw
register debugging, RDS encoder/scheduler/FIFO feeding, or production UI.
Audio profiles are persisted but do not process audio. Mock source metadata is
available to injected tests; there is no real metadata extraction.

Saved `enabled` values survive restart, but actual TX state initially returns to
OFF. Automatic desired-state startup restoration, reconnect/recovery, systemd
units and target performance/reliability measurements belong to Milestone 6.
No claim of Linux/Pi hardware or performance validation is made from Windows tests.

There are no architectural deviations. The optional runtime RT GET endpoint,
configurable default FM band, raw RF-level representation, whole-object PATCH
semantics and active-source edit restriction are documented implementation choices
where the specification leaves details open.
