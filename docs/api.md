# FMheadend 1.0.0 REST API

FMheadend exposes a versioned JSON API from the `fmheadendd` HTTP listener. The
Angular WebUI uses the same interface as external clients.

## 1. API fundamentals

| Item | Value |
|---|---|
| Example origin | `http://127.0.0.1:8080` |
| API prefix | `/api/v1` |
| Payload format | JSON, except empty `204 No Content` responses |
| API schema | `GET /openapi.json` |
| Swagger UI | `GET /docs` |
| ReDoc | `GET /redoc` |
| Authentication | Optional HTTP Basic Authentication for the entire HTTP application |

The listen address, port and authentication mode are startup options described in
the [configuration reference](configuration.md). The `/api/v1` path is the API
version; there is no runtime version negotiation.

Request models reject unknown fields, non-finite numbers and values outside their
declared constraints. `PATCH` requests change only supplied fields. JSON responses
use enum values such as `HTTP`, `DIGITAL`, `RUNNING` and `ON` exactly as shown in
the OpenAPI schema.

FastAPI also registers `/docs/oauth2-redirect` for the Swagger UI. FMheadend does
not use OAuth. When WebUI assets are configured, non-API `GET` requests may serve a
static asset or a recognized Angular route; this SPA fallback is not part of the
REST API. No WebSocket interface is exposed.

### 1.1 Authentication

When `fmheadendd` starts without `--passwd-file`, HTTP authentication is disabled.
When it starts with that option, every HTTP route is protected, including API
reads, writes, OpenAPI, Swagger UI, static files and SPA routes.

Use an Apache-compatible bcrypt `htpasswd` entry:

```sh
curl -u 'username:password' http://127.0.0.1:8080/api/v1/status
```

Invalid or missing credentials receive `401 Unauthorized` with:

```http
WWW-Authenticate: Basic realm="FMheadend", charset="UTF-8"
Cache-Control: no-store
```

The `401` response has an empty body. HTTP Basic credentials are only protected
against interception when used through HTTPS or another trusted encrypted
transport.

### 1.2 Cached observations

Most configuration commands execute on the backend's serialized control worker.
Potentially slow Source probes and filesystem discovery use a separate bounded
worker. A saturated control or discovery path can return `503` rather than queue
work without limit.

The principal status and inventory reads are published snapshots. They do not
perform Controller, mux, database or media I/O in the HTTP request:

- `GET /api/v1/status` includes snapshot and Controller-observation age fields.
- `GET /api/v1/sources/{id}/status` includes `observation_age_s` and
  `observation_stale`.
- Source, Modulator, Audio Profile and system-information inventories include
  `Cache-Control: no-store` and `X-Observation-Age-Seconds` response headers.

An HTTP `200` therefore proves that the backend answered; it does not prove that a
Controller observation is fresh. A successful lifecycle request also does not
replace later Controller-observed state. Clients should reconcile requests with
subsequent status snapshots.

## 2. Endpoint inventory

The application registers 38 functional operations under `/api/v1`.

### 2.1 System and status — 5 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/status` | Return the cached system and physical-Modulator runtime snapshot. |
| `GET` | `/api/v1/system/info` | Return application, Controller, capability, frequency-plan and media-backend information. |
| `GET` | `/api/v1/system/logs` | Return the newest 100 in-process backend log entries since startup. |
| `GET` | `/api/v1/system/config` | Read persistent system configuration. |
| `PATCH` | `/api/v1/system/config` | Change the system-default Audio Profile and apply it to active inheriting Sources. |

`SystemConfig` contains `default_audio_profile_id` and `global_stopped`.
`global_stopped` is read-only through this resource and is changed by START ALL,
STOP ALL or the documented individual-START behavior.

`GET /api/v1/system/info` includes:

| Field | Meaning |
|---|---|
| `fmheadend_version` | Installed backend and bundled-WebUI application version. |
| `controller_protocol_version` | Negotiated Controller protocol version, or `null`. |
| `controller_firmware_version` | Reported Controller firmware version, or `null`. |
| `hardware_target` | Reported firmware-compatible hardware target, or `null`. |
| `modulator_count` | Last verified physical TX count, 1–8, or `null` before negotiation. |
| `controller_capability_stale` | Whether retained Controller capability data is no longer current. |
| `frequency_min_khz`, `frequency_max_khz`, `frequency_step_khz` | Controller-reported hardware tuning capability, or `null`. |
| `frequency_plan` | Region number/name and the current WebUI selection range/raster. |
| `database_schema_version` | SQLite schema identifier. |
| `media_backend` | Active media implementation, such as `GStreamer` or `MOCK`. |
| `mocked` | Whether the Controller client is mocked. |

Each `/system/logs` entry contains UTC `timestamp`, `severity`, `logger` and a
bounded `message`. This is an in-memory diagnostic view, not the system journal.
There is no separate health-check endpoint; use `/status` and its freshness fields.

### 2.2 Sources — 8 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/sources` | List all saved Sources. |
| `POST` | `/api/v1/sources` | Create a Source; returns `201 Created`. |
| `POST` | `/api/v1/sources/test` | Test an unsaved Source configuration without persisting it. |
| `GET` | `/api/v1/sources/{id}` | Read one saved Source. |
| `PATCH` | `/api/v1/sources/{id}` | Persist supplied fields and apply audio-affecting changes to its active shared pipeline. |
| `DELETE` | `/api/v1/sources/{id}` | Delete an unreferenced Source; returns `204 No Content`. |
| `POST` | `/api/v1/sources/{id}/test` | Test the saved configuration of one Source; no request body is required. |
| `GET` | `/api/v1/sources/{id}/status` | Return the cached runtime state and diagnostics of one Source. |

Source identifiers are positive integers assigned by SQLite. Source types are
`HTTP`, `UDP`, `RTP`, `ALSA`, `LOCAL` and `PODCAST`. Common writable fields are:

| Field | Meaning |
|---|---|
| `name` | Non-empty display name, at most 200 characters. |
| `type` | Source type; determines validation of `config`. |
| `config` | Type-specific object described below. |
| `use_default_audio_profile` | `true` to resolve the current system default dynamically. |
| `audio_profile_id` | `default/name.json` or `custom/name.json` when not inheriting; otherwise `null`. |
| `input_gain_db` | Source gain in dB; default `0`, maximum `20`. |
| `rds_profile` | Optional Source-owned RDS profile object. |
| `gst_options` | Optional property maps for `source`, `queue` and `resample`. |

Reads additionally return `id`, `timestamp` and the internal `rds_profile_id` when
a Source-owned RDS profile exists. The `rds_profile_id` is not writable.

#### 2.2.1 Type-specific Source configuration

| Type | `config` fields |
|---|---|
| `HTTP` | Required `uri` using HTTP or HTTPS; optional `radio_browser_uuid`; `buffer_ms` default `3000`; `connect_timeout_ms` default `10000`. |
| `UDP` | Required `port`, `format`, `sample_rate`, `channels`; `host` default `0.0.0.0`; optional `multicast_group`. Formats: `S16LE`, `S16BE`, `S24LE`, `S24BE`, `S32LE`, `S32BE`, `F32LE`, `F32BE`, `U8`. |
| `RTP` | Required `port`, `codec`, `payload_type`; `host` default `0.0.0.0`; optional `multicast_group`, `clock_rate`, `channels`, `codec_data`. Codecs: `PCM`, `MP3`, `AAC`, `OPUS`. |
| `ALSA` | Required `device`; `channel_selection` is `stereo` (default), `mono`, `left` or `right`. |
| `LOCAL` | Required `mode` (`folder` or `playlist`) and absolute host `path`; `order` is `sequential` (default) or `random`; `recursive` defaults to `false`. |
| `PODCAST` | Required `feeds` array of 1–32 objects; `schedule_mode` is `SMART` (default) or `RANDOM`; `refresh_interval_minutes` default `30`; `replay_cooldown_hours` default `24`. |

Each Podcast feed object requires an HTTP(S) `url` and may set non-negative
`max_age_days` and `max_episodes`. Feed URLs containing embedded credentials are
rejected.

`PATCH /sources/{id}` returns the complete saved Source plus:

```json
{
  "saved": true,
  "applied": false,
  "apply_error": "runtime pipeline replacement failed"
}
```

The fields above illustrate the apply result; the actual response also contains
the complete Source. `saved:true` with `applied:false` means persistence succeeded
but the active runtime could not be rebuilt. Idle Sources remain idle.

Source test responses contain `ok`, `mocked`, `message` and available diagnostics.
Audio probes can report `codec`, `sample_rate`, `channels`, `output_caps`,
`input_bitrate_bps`, `station_name` and `stream_title`. LOCAL tests report either
`local_file_count` or `playlist_entry_count` and do not decode audio. Podcast tests
return aggregate and per-feed discovery/eligibility results. Tests do not save a
Source, change podcast playback history or operate a Modulator.

### 2.3 Modulators and transmission control — 8 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/modulators` | List configurations for currently known physical TX IDs. |
| `GET` | `/api/v1/modulators/{id}` | Read one physically available Modulator configuration. |
| `PATCH` | `/api/v1/modulators/{id}` | Save configuration while the TX is confirmed off, except for hot-applied RDS-only changes. |
| `POST` | `/api/v1/modulators/{id}/save-and-resume` | Save changes, stopping and restarting that TX when required; reports partial outcomes. |
| `POST` | `/api/v1/modulators/{id}/start` | Persist `enabled=true` and request startup. |
| `POST` | `/api/v1/modulators/{id}/stop` | Persist `enabled=false` and request RF off. |
| `POST` | `/api/v1/modulators/start-all` | Clear the global hold and attempt eligible saved-enabled physical Modulators. |
| `POST` | `/api/v1/modulators/stop-all` | Set the persistent global hold and attempt RF off for every physical Modulator. |

The database always contains logical Modulator IDs `0`–`7`. Public list and control
operations expose only the contiguous Controller-reported physical inventory
`0..N-1`. Before capability negotiation, the list can be empty and single-resource
operations fail safely. Retained inventory after communication loss is marked stale
through system status and system information; it is not proof of current Controller
connectivity.

Principal Modulator configuration fields are:

| Field | Values and behavior |
|---|---|
| `frequency_khz` | Integer tuning frequency or `null`; validated against verified Controller range/raster before hardware use. |
| `audio_mode` | `DIGITAL` or `ANALOG`. Analog mode clears `input_source_id`. |
| `input_source_id` | Source ID required for an enabled digital Modulator. |
| `gain_mode` | `AUTO` or `MANUAL`. |
| `analog_gain_db` | Analog input gain. |
| `soft_clipping` | Controller-side transmitter soft-clipping selection. |
| `stereo` | Stereo enable. |
| `preemphasis_us` | `50` or `75`. |
| `rf_attenuation_db` | `null` or `0`–`22.5` dB in `1.5` dB steps. |
| `enabled` | Persistent per-Modulator selection; changed through lifecycle operations, not `PATCH`. |
| `rds_enabled`, `rds_follow_source`, `rds_override_profile` | Persistent RDS enablement and profile selection. |

Responses also project the effective RDS fields and return `id`, `timestamp` and
`rds_override_profile_id`. A non-RDS `PATCH` requires confirmed RF OFF and released
Source ownership. Attempting to change `enabled` through `PATCH` returns
`409 LIFECYCLE_REQUIRED`; use START or STOP.

`POST /modulators/{id}/save-and-resume` returns:

| Field | Meaning |
|---|---|
| `saved` | Whether the new configuration was persisted. |
| `stop_confirmed` | Whether a required RF-off step was confirmed. |
| `restarted` | Whether a previously running TX was started again. |
| `failed_stage` | `stop`, `save`, `start` or `null`. |
| `message` | Human-readable outcome. |
| `configuration` | Saved configuration, or `null` if saving did not complete. |

Expected stage failures are represented in this response and can still use HTTP
`200`; inspect the fields rather than relying on the status code alone.

START and STOP change persistent desired enablement before the hardware operation.
If the operation later returns `503` because its outcome is pending or uncertain,
clients must poll status. In particular, initial LOCAL discovery can return
`LOCAL_DISCOVERY_PENDING`; RF stays off until the Source becomes ready, while the
deferred lifecycle may later complete. A terminal LOCAL startup failure clears the
saved enable intent.

Individual START has special behavior while STOP ALL is active: it atomically
clears `global_stopped`, enables only the selected logical Modulator and disables
the other saved Modulator selections before attempting that TX.

Bulk operations return an array with one row per physical TX considered. Each row
is either a `ModulatorStatus` object or:

```json
{"id": 3, "error": "DEPENDENCY_UNAVAILABLE", "message": "Controller unavailable"}
```

Per-TX failures therefore do not turn the whole response into a non-`200` status.
STOP ALL preserves every Modulator's saved `enabled` value. START ALL does not set
all Modulators enabled; it attempts only physical, saved-enabled Modulators with a
sufficient configuration.

### 2.4 Runtime RDS — 3 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `PUT` | `/api/v1/modulators/{id}/rds/rt` | Set a runtime RadioText override, optionally with a TTL. |
| `GET` | `/api/v1/modulators/{id}/rds/rt` | Read effective runtime RadioText, origin and remaining override TTL. |
| `DELETE` | `/api/v1/modulators/{id}/rds/rt` | Clear the runtime override; returns `204 No Content`. |

Set an override with:

```sh
curl -sS -X PUT http://127.0.0.1:8080/api/v1/modulators/0/rds/rt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Live programme","ttl_s":300}'
```

The response is shaped as follows:

```json
{"text":"Live programme","origin":"override","remaining_ttl_s":300.0}
```

`text` is at most 64 printable characters; an empty string is valid. When
supplied, `ttl_s` must be a finite number greater than zero; it has no configured maximum. Omit it or send
`null` for an override without automatic expiry. `origin` is `override`,
`metadata` or `fallback`; `remaining_ttl_s` is non-null only for an expiring
override. After expiry, `GET` returns the selected metadata or fallback text.
Overrides are runtime-only and do not survive a backend restart.

Persisted RDS content is part of Source and Modulator configuration, not a separate
top-level REST resource. An RDS profile object contains `rds_pi`, `rds_ecc`,
`rds_pty`, `rds_tp`, `rds_af_khz`, `rds_ps`, `rds_rt`, `rds_rt_enabled`,
`rds_rt_termination` and `rds_metadata_to_rt`. Each `rds_ps` entry contains
`text` and `duration_s`. A Source stores this object as `rds_profile`; a Modulator
uses `rds_follow_source` or its own `rds_override_profile`.

Changing Source-owned RDS content hot-applies it to following Modulators. RDS-only
Modulator changes do not restart RF or audio. The effective text in a status
snapshot is the selected text at publication time; already queued Controller RDS
groups can lag behind it.

### 2.5 Audio Profiles — 7 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/audio-profiles/files` | List valid built-in and custom profiles. |
| `POST` | `/api/v1/audio-profiles/files/custom` | Create a custom profile from a complete schema-1 document; returns `201 Created`. |
| `GET` | `/api/v1/audio-profiles/files/dsp` | Return editor metadata, stage choices, implementation availability, schemas and defaults. |
| `GET` | `/api/v1/audio-profiles/files/{kind}/{filename}` | Read one profile and its summary. |
| `PUT` | `/api/v1/audio-profiles/files/custom/{filename}` | Replace an existing custom profile and rebuild active users. |
| `POST` | `/api/v1/audio-profiles/files/{kind}/{filename}/clone` | Clone a built-in or custom profile into a new custom file; returns `201 Created`. |
| `DELETE` | `/api/v1/audio-profiles/files/custom/{filename}` | Delete an unreferenced custom profile; returns `204 No Content`. |

Profile IDs are stable relative paths such as `default/basic_filtering.json` and
`custom/station_voice.json`. `{kind}` is `default` or `custom`; `{filename}` is a
restricted `.json` filename. Built-in profiles are read-only but clonable. A custom
profile cannot be deleted while it is the system default or directly referenced by
a Source.

Create and replace operations accept a complete Audio Profile document with
`schema_version: 1`, `name`, `description` and exactly the twelve supported `stages`.
Use `GET /audio-profiles/files/dsp` and the OpenAPI schema to construct it. The
backend does not silently bypass unavailable selected DSP elements.

Clone a profile without sending its stage data:

```sh
curl -sS -X POST \
  http://127.0.0.1:8080/api/v1/audio-profiles/files/default/basic_filtering.json/clone \
  -H 'Content-Type: application/json' \
  -d '{"name":"Station processing"}'
```

The clone response contains the generated profile identifier in `path`. Its
filename includes a timestamp and may include a collision suffix, so clients must
use the returned value rather than derive a filename from the profile name.

Profile replacement returns `saved`, `applied` and `restart_errors`. Saving is
atomic; `applied:false` means the file was saved but at least one active Source
could not be rebuilt. Creating or cloning an unused profile has no runtime effect.

Set the system default through the system configuration resource:

```sh
PROFILE_ID='REPLACE_WITH_PATH_FROM_CLONE_RESPONSE'
curl -sS -X PATCH http://127.0.0.1:8080/api/v1/system/config \
  -H 'Content-Type: application/json' \
  -d "{\"default_audio_profile_id\":\"${PROFILE_ID}\"}"
```

The response contains the saved system configuration plus `applied` and a
`restart_errors` map keyed by Source ID. Active Sources inheriting the default are
rebuilt; fixed-profile and idle Sources are unchanged.

### 2.6 Input discovery — 3 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/alsa/capture-devices` | List capture PCM devices discovered through Linux ALSA metadata. |
| `GET` | `/api/v1/filesystem/locations` | List configured LOCAL-media browser roots and availability. |
| `GET` | `/api/v1/filesystem/browse?path={path}&mode={mode}` | Browse an allowed directory for folder or playlist selection. |

ALSA discovery returns `{"devices":[...]}`. Each device has `pcm`, `label`,
`card_id` and `device`. Enumeration does not open the capture device or guarantee
its format support.

Filesystem locations return `name`, `path` and `available`. Browsing requires an
absolute path inside a configured media-browser root and `mode=folder` or
`mode=playlist`. The response contains resolved `path`, containing `root`,
`can_go_up`, and entries with `name` and `type`. Folder mode lists directories;
playlist mode additionally lists `.m3u` and `.m3u8` files. These endpoints are
read-only and do not create or test a Source.

Only one discovery operation runs at a time. A concurrent request can receive
`503 DISCOVERY_BUSY`; a job exceeding the bounded HTTP wait receives
`503 DISCOVERY_TIMEOUT` while the worker retains ownership until that job finishes.

### 2.7 Radio Browser directory — 4 operations

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/radio-browser/stations/search` | Search the external Radio Browser directory. |
| `GET` | `/api/v1/radio-browser/stations/{station_uuid}` | Read one station by UUID. |
| `GET` | `/api/v1/radio-browser/countrycodes` | List country-code options and station counts. |
| `GET` | `/api/v1/radio-browser/tags` | List or search tag options and station counts. |

Search query parameters:

| Parameter | Default | Constraint |
|---|---:|---|
| `name` | empty | At most 200 characters. |
| `countrycode` | omitted | Two ASCII letters; normalized to uppercase. |
| `tag` | omitted | At most 100 characters. |
| `offset` | `0` | `0`–`100000`. |
| `limit` | `25` | `1`–`100`. |
| `hidebroken` | `true` | Boolean. |

`countrycodes` accepts `offset` and `limit` (`250` by default, maximum `250`).
`tags` accepts `name`, `offset` and `limit` (`25` by default, maximum `100`).

Station results contain identity and directory metadata: `stationuuid`, `name`,
`url`, `url_resolved`, selected `playable_url`, country, tags, codec, nominal
bitrate, favicon, last-check state and HLS flag. Directory reads do not persist or
operate Sources. To link a station, create or patch an ordinary HTTP Source with
its `uri` and optional `radio_browser_uuid`.

Podcast operation is intentionally part of the Source API: there are no separate
Podcast or playback-history endpoints. There are also no REST firmware-update,
direct Controller-command, mux-control, audio-telemetry or debug endpoints. Those
interfaces remain outside the public HTTP API.

## 3. Monitoring workflow

Use these resources together:

| Resource | What it establishes |
|---|---|
| `/api/v1/status` | Cached system health, Controller/mux observations and one runtime row per physical Modulator. |
| `/api/v1/modulators` | Cached persistent configuration for the same physical inventory. |
| `/api/v1/sources` | Cached saved Source inventory. |
| `/api/v1/sources/{id}/status` | Cached Source-specific media and PCM state. |
| `/api/v1/system/info` | Negotiated capability inventory and application/Controller identity. |
| `/api/v1/system/logs` | Recent backend messages useful after a failed operation. |

`GET /api/v1/status` returns a `system` object and `modulators` array. Important
system fields include:

| Field | Interpretation |
|---|---|
| `system_state` | Aggregate `RUNNING` or `DEGRADED` presentation state. |
| `global_stopped` | Persistent global transmission hold. |
| `snapshot_age_s`, `snapshot_stale` | Age and validity of the published backend snapshot. |
| `controller_communication_state` | `AVAILABLE`, `UNAVAILABLE` or `INCOMPATIBLE`. |
| `controller_observation_age_s`, `controller_observation_stale` | Age and freshness of the last successful Controller status observation. |
| `controller_state` | Controller-reported state, or `UNKNOWN` before an observation. |
| `mux_state`, `mux_transport`, `mux_transport_state` | Mux service and confirmed transport state. Transport is `null` before it has been observed. |
| `mux_frames_sent`, `mux_late_frames`, `mux_transport_errors` | Mux delivery counters. |
| `spi_errors`, `i2c_errors` | Transport error counters. |
| `last_error` | Current aggregate diagnostic text, if any. |

Each Modulator runtime row separates `tx_state` from `hardware_tx_state` and
includes Source state/errors, RDS state/errors, mux underrun/overrun and PCM byte
counters, Controller underrun/FIFO diagnostics and scoped last errors. Treat
`hardware_tx_state` as observed data only when the Controller observation is fresh.

Source status uses `IDLE`, `CONNECTING`, `BUFFERING`, `RUNNING` or `ERROR`. It can
include codec/input format, bitrate, metadata, reconnect count, output caps, PCM
queue counters and `last_error`. A Source being `RUNNING` does not prove RF is on.

## 4. Source-to-transmission workflow

Create an HTTP Source from an unsaved configuration. The `.invalid` example URL
is deliberately nonfunctional and must be replaced with a real media URL:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"Example stream",
    "type":"HTTP",
    "config":{
      "uri":"https://stream.example.invalid/live",
      "buffer_ms":3000,
      "connect_timeout_ms":10000
    },
    "use_default_audio_profile":true,
    "audio_profile_id":null,
    "input_gain_db":0,
    "gst_options":{}
  }'
```

The response is the saved Source with a dynamically assigned integer `id`. Set
`SOURCE_ID` to that returned value, then configure an off physical Modulator:

```sh
SOURCE_ID=REPLACE_WITH_RETURNED_ID
curl -sS -X PATCH http://127.0.0.1:8080/api/v1/modulators/0 \
  -H 'Content-Type: application/json' \
  -d "{\"audio_mode\":\"DIGITAL\",\"input_source_id\":${SOURCE_ID},\"frequency_khz\":99500}"
```

Then request startup and monitor both TX and Source state:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/modulators/0/start
curl -sS http://127.0.0.1:8080/api/v1/status
curl -sS "http://127.0.0.1:8080/api/v1/sources/${SOURCE_ID}/status"
```

The frequency must satisfy the negotiated Controller hardware capability. Regional
frequency plans are WebUI presets rather than backend regulatory enforcement.
Multiple digital Modulators can reference one Source; the backend shares one active
decoder and Audio Profile pipeline while maintaining per-consumer PCM queues.

Stop the individual TX with:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/modulators/0/stop
```

## 5. Global control workflow

Set the persistent STOP ALL hold:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/modulators/stop-all
```

This preserves saved per-Modulator `enabled` selections, attempts every currently
physical TX even if another fails, and prevents startup/recovery from restoring RF.

Clear the hold and attempt the saved eligible selection:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/modulators/start-all
```

Inspect every returned row and then poll `/status`; neither a bulk HTTP `200` nor an
empty per-row error field substitutes for a fresh Controller observation.

## 6. Persistence and runtime effects

| Operation | Persistent effect | Runtime effect |
|---|---|---|
| Source create/update/delete | SQLite Source and owned RDS data | Active audio-affecting updates rebuild the shared Source pipeline. |
| Modulator `PATCH` | SQLite Modulator and owned RDS data | RDS-only changes hot-apply; other changes require confirmed OFF. |
| Individual START/STOP | Changes saved `enabled` | Attempts Source/mux/Controller lifecycle action. |
| START ALL/STOP ALL | Changes `global_stopped`; preserves enabled selections | Attempts eligible start or physical stop per TX. |
| Individual START during STOP ALL | Clears `global_stopped`; selects only that Modulator as enabled | Attempts that selected TX. |
| `save-and-resume` | Saves only if its save stage is reached | May stop and restart one TX; reports each outcome. |
| Audio Profile create/clone/delete | Changes custom profile files | No pipeline change unless an active referenced profile is replaced or the default changes. |
| Audio Profile replace | Atomically replaces a custom profile file | Rebuilds active Sources resolving to it. |
| System default profile PATCH | Changes SQLite system configuration | Rebuilds active inheriting Sources. |
| Runtime RadioText PUT/DELETE | None | Changes in-memory effective RT until cleared, expired or restarted. |
| Source test/discovery/Radio Browser | None | Performs bounded read/probe work without changing active playback or RF. |

Backend startup options such as the HTTP listener, Controller device/address,
filesystem roots and authentication are not REST settings and require process
configuration. See [Configuration](configuration.md).

## 7. Errors and partial outcomes

Domain and validation failures normally use:

```json
{
  "error": "FREQUENCY_REQUIRED",
  "message": "Configure a transmitter frequency before enabling a modulator"
}
```

| HTTP status | Typical meaning |
|---:|---|
| `200` | Read or operation completed; inspect partial-result bodies where documented. |
| `201` | Source or custom Audio Profile created. |
| `204` | Source/profile deletion or runtime RT clear completed; no body. |
| `401` | Basic Authentication challenge; empty body. |
| `403` | Read-only profile or disallowed/unreadable browser path. |
| `404` | Resource, physical TX, station, profile or path not found. |
| `409` | Resource conflict, invalid lifecycle transition or incomplete eligible configuration. |
| `422` | Request/schema validation or invalid frequency/profile/path content. |
| `500` | Sanitized unexpected backend error. |
| `503` | Controller, mux, Source, storage or external directory unavailable; bounded worker busy/timeout. |

Pydantic/FastAPI validation errors are normalized to:

```json
{
  "error": "VALIDATION_ERROR",
  "message": "body.config: Field required"
}
```

Framework 404/405 and static-route failures use `HTTP_<status>` error codes.
Unexpected exceptions are
logged and returned as `500` with `INTERNAL_ERROR` and the generic message
`Internal server error`; private exception details are not exposed.

Four successful HTTP patterns require body inspection:

1. Bulk START/STOP returns per-TX status or error rows.
2. `save-and-resume` reports `failed_stage` without necessarily changing HTTP status.
3. Source/profile/system saves can persist successfully while `applied` is false.
4. Source tests can return `200` with `ok:false` for a completed negative probe.

Network timeouts are intrinsically ambiguous: the server may have completed an
operation after the client stopped waiting. Re-read configuration and cached status
rather than immediately repeating a lifecycle command.

## 8. Related interfaces

The public REST API does not expose the Controller I²C wire protocol or the muxd
Unix-socket protocols. See:

- [Architecture](architecture.md) for process and interface boundaries.
- [Operations](operations.md) for operator workflows and recovery.
- [Audio Sources](sources.md) for input-specific behavior.
- [Audio processing and Audio Profiles](audio.md) for DSP details.
- [Controller protocol](controller-protocol.md) for the firmware control interface.
- [RP2354 firmware update](rp2354-firmware-update.md) for provisioning and OTA.
