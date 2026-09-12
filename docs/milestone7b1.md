# Milestone 7B1: Sources management

M7B1 adds Sources management to the existing Angular shell and Dashboard. Its reviewed
model corrections make analog input Modulator-owned and remove duplicate HTTP
User-Agent configuration. No M7B2 forms, RP firmware, RDS feature or mux protocol
change is included.

## Structure and existing API

- `/sources`: SourcesList, compact rows with name/type/endpoint, actual runtime state,
  persistent Modulator reference count, Edit/Test/Delete and named inline confirmation.
- `/sources/new`, `/sources/:id`: SourceEditor reactive form with dirty navigation and
  browser-unload protection. TestResult is shared between list and editor.
- SourceApi contains explicit request/response types and REST calls. Source configuration
  remains in DashboardState's existing shared snapshot, not a second source cache.
- List runtime reads follow successful shared five-second refreshes, at most four
  concurrent requests. Leaving the page cancels them; there is no extra polling timer.
- Confirmed mutations wait for any older snapshot, then refresh shared data. Editor
  contents are loaded once and never overwritten by background polling.

| Endpoint | Purpose |
|---|---|
| GET `/api/v1/sources` | Shared inventory |
| GET `/api/v1/sources/{id}` | Load editor |
| POST `/api/v1/sources` | Create |
| PATCH `/api/v1/sources/{id}` | Edit |
| DELETE `/api/v1/sources/{id}` | Delete (204 success) |
| POST `/api/v1/sources/test` | Test unsaved configuration |
| POST `/api/v1/sources/{id}/test` | Test saved configuration |
| GET `/api/v1/sources/{id}/status` | Source runtime |
| GET `/api/v1/audio-profiles` | Select existing profile |
| GET `/api/v1/modulators`, `/api/v1/status` | Existing shared snapshot |

No new endpoint was required. Active-source edits and referenced-source deletion
remain rejected by the backend. The UI displays the reason without unassigning or
cascading. Failed operations preserve form values. Tests never save or rename saved
sources. Save and Test prevent duplicate pending operations.

## Configuration and presentation

InputSources are reusable digital inputs only. Common fields: name, type, enabled,
optional existing Audio Profile and advanced source/queue/resample GStreamer options.

| Type | Per-type fields |
|---|---|
| HTTP / HLS | uri, buffer_ms, connect_timeout_ms |
| UDP | host, port, multicast_group, format, sample_rate, channels |
| RTP | host, port, multicast_group, codec, payload_type, clock_rate, channels, codec_data |
| ALSA | device, sample_rate, channels |

Type switching clears incompatible config and advanced options. Optional blanks defer
to existing defaults; helper text reads "Optional. Leave blank to use the default."
Required fields, URL, numeric range and codec hex syntax receive frontend checks;
backend validation remains authoritative. ALSA uses a device string, not enumeration.

The Sources list/header share a left-aligned 1200px maximum width. HTTP URL fills its
own row beside a compact Test stream button, wrapping at narrow widths. Other HTTP
fields remain below. The existing dark, compact M7A design is retained. List URLs omit
credentials/query and truncate visually with an inspectable title.

Runtime shows actual IDLE/CONNECTING/BUFFERING/RUNNING/ERROR; unavailable state is
UNKNOWN, never inferred IDLE. Backend loss retains configuration with a stale warning.

## Model and migration corrections

The canonical architecture defines the exact rules; M7B1 introduced SQLite schema **6**. The subsequent RP audio-control prerequisite adds
schema **7** audio-control defaults; see the canonical architecture.

- Schema 5 removes legacy ANALOG InputSources. Referencing modulators select their
  own physical ANALOG jack and clear input_source_id. Shared legacy references become
  independent physical jacks, not a routing matrix. Unreferenced analog records are
  removed; Audio Profiles and unrelated configuration are preserved. Conflicting
  SQL/JSON references abort that migration transaction. Digital/default modulators
  use DIGITAL, with normal source validation and sharing. No hidden assignment returns
  after switching back from ANALOG. RDS remains Modulator-owned and operational.
- Schema 6 removes HTTP config.user_agent. Its value moves to the existing advanced
  source `user-agent` property only when absent; an existing advanced value retains
  the previous effective precedence. IDs, timestamps, relationships and other fields
  are unchanged. New requests with the obsolete field are rejected.
- Defaults, foreign keys, fixed physical IDs and the existing versioned migration chain
  remain. Successful steps commit transactionally; reopening an upgraded DB is idempotent.

Dashboard uses audio_mode and shows Audio input: Analog without Source IDLE or Digital
audio Idle. Analog source runtime is null, including unavailable-RP startup. RP
SET_TX_CONFIG wire values remain DIGITAL=0 and ANALOG=1. No Modulator editor was added.

## HTTP test diagnostics

Successful tests preserve codec, input rate/channels, normalized technical caps and
now-playing title, and add optional:

- `station_name`: ICY `icy-name` from GStreamer `http-headers` / `response-headers`,
  with public context-managed structure access. Generic title/artist/organization
  are not station identity. No URL inference/provider rules. Missing identity is null,
  including HLS transports without this header. Text is printable, trimmed and bounded
  to the existing Name limit.
- `input_bitrate_bps`: existing positive GStreamer bitrate/nominal-bitrate metadata,
  not PCM-derived or measured network throughput. The existing tag collector prefers
  bitrate within each message; the latest available value is reported. UI uses decimal
  kb/s rounded to an integer, e.g. `Bitrate: ~192 kb/s`, and omits unavailable values.
  No whole-stream VBR average is claimed. The first-audio probe interval is insufficient
  for reliable byte-rate averaging, so no measurement fallback or delay is added.

HTTP tests can run with an empty form Name by using a temporary unsaved probe label
in the existing API request. A successful station result fills only a currently blank
Name, marks it dirty, and never overwrites a nonblank custom/saved name. Save still
requires a valid Name. List testing never mutates persistence. Existing User-Agent
handling, polling, station/title semantics and normal playback architecture remain.

Tests have a 60-second frontend timeout, with no automatic retry. Native connection
wait is already clamped to 30 seconds; normal resource calls use 15 seconds. Outcome-
unknown failures are reported honestly. Source test success does not prove audible
programme content or physical RF transmission.

## Development and validation commands

Use Node 24 and npm as documented for M7A. Angular reactive forms is the only added
frontend dependency; there is no UI framework, state framework or E2E dependency.

```sh
cd webui
npm ci
npm start
npm test -- --include src/app/features/sources/sources.spec.ts
npm test
npm run typecheck
npm run build
npm run format:check
```

The build checks strict Angular templates. Windows uses a local temporary validation
copy because esbuild cannot execute from the NAS share. Existing npm install-script
warnings remain; prebuilt binaries work without globally allowing scripts.

## Isolated manual smoke

Never use the normal configuration DB/runtime. Start native mux with mock SPI and
fmheadendd with mock RP in a dedicated temporary directory:

```sh
smoke_dir=$(mktemp -d /tmp/fmheadend-m7b1.XXXXXX)
mkdir "$smoke_dir/run"
fmheadend-muxd --transport mock --runtime-dir "$smoke_dir/run" &
mux_pid=$!
fmheadendd --mock-rp --database "$smoke_dir/test.db" \
  --mux-runtime-dir "$smoke_dir/run" --host 127.0.0.1 --port 18080 &
backend_pid=$!
```

Point an untracked Angular dev proxy at this backend, using SSH forwarding if needed.
Serve local MP3 audio encoded at 192 kb/s with an HTTP fixture supplying `icy-name`.
No public internet stream is required.

1. Create HTTP with Name blank; Test stream. Confirm station autofill, MP3 codec,
   input rate/channels, approximate bitrate and exact S16LE/48000/stereo output caps.
2. Enter a deliberate Name and retest; verify no overwrite. Save, edit, and use list
   Test; verify persisted names do not change. Exercise advanced user-agent JSON.
3. Create UDP/RTP/ALSA using the fields above and inspect their saved serialization.
   Confirm no ANALOG or normal User agent field, and no backend-default helper jargon.
4. Configure isolated digital and analog modulators through the existing API; Dashboard
   must show eight cards, source-name resolution, digital PCM flow, analog input and
   working START/STOP. Analog RDS remains active without a Source.
5. Verify dirty navigation cancellation, and retained edits during backend stop/restart
   with automatic recovery. Inspect list and HTTP editor at desktop/narrow/ultrawide widths.
6. Stop recorded smoke process PIDs, wait for exit, and remove only the verified temporary
   directory. Do not touch normal database or service processes.

## Remaining scope

Modulator/RDS editing, Audio Profile management, System/Diagnostics and production UI
hosting remain for M7B2/later. No future RDS update-from-source feature, RP firmware,
Bandscan or M6B work was added. Physical RP/QN transmission is not tested.


## Final review and validation

Review corrected the analog startup cache (source_state is null even before an
unavailable RP can restore configuration), removed imports made obsolete by analog
source removal, and consolidated stale README/milestone statements. A combined
schema-4-to-6 test verifies digital Sources, legacy shared/unused analog records,
HTTP effective User-Agent, Audio Profiles, Modulators, RDS, timestamps and System
configuration survive as intended. Per-step atomic rollback and idempotent reopen
coverage passed. No migration rule or wire protocol was redesigned.

| Check | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Complete Python suite on Raspberry Pi OS | 334 | 0 | 0 |
| Native mux CTest | 8 | 0 | 0 |
| Complete frontend suite on Windows | 66 | 0 | 0 |

Strict TypeScript, strict Angular template compilation, production build, Prettier
and git diff whitespace checks passed. Production initial bundle: 367.95 kB raw,
96.53 kB estimated transfer. No build warnings; Python emitted the two existing
Starlette/httpx and anyio deprecation warnings.

Real headless Chrome smoke passed against isolated native Pi fmheadendd/GStreamer/mux
with mock RP/SPI. A generated local 192 kb/s MP3 and ICY header produced Station:
Local Validation FM, MPEG-1 Layer 3, 48000 Hz / 2 channels, Bitrate: ~192 kb/s, and
exact S16LE/48000/stereo output caps. Blank-name autofill, manual-name preservation,
saved/unsaved tests, create/edit, advanced User-Agent and UDP/RTP/ALSA serialization
passed. Dashboard digital PCM flow, analog START/STOP and RDS passed. Dirty navigation
cancellation and backend stop/restart retained form edits. Layout was checked at
3440/1440/390 px, with editor/list screenshots visually reviewed. An early smoke
assertion was corrected to await the normal shared status poll; no UI change was needed.
All temporary Pi processes/data and local smoke server/tunnel were cleaned up.

Native media regression includes local HTTP/RTP/UDP and the configured ALSA capture
device; no public radio stream is required. No physical RP/QN or RF transmission was
tested. RP wire protocol and mux frame format are unchanged. All changes remain
uncommitted; no M7B2 functionality was added.
