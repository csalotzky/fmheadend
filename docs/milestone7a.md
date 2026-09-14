# Milestone 7A: Angular operational Dashboard

M7A implements the Web UI foundation and operational Dashboard only. The canonical
visual/interaction specification remains [webui-spec.md](webui-spec.md). The focused Modulator identity correction removes the redundant `name` field
from backend/API/frontend contracts in the current pre-V1 schema 1. RP/mux/RDS behavior
is unchanged.

## Structure and routes

`webui/` is an Angular 22 standalone TypeScript application with Router, HttpClient,
Angular signals and the CLI's Vitest/jsdom runner. There is no SSR, NgRx, component
framework, authentication, WebSocket, BFF or browser-persisted desired state.

- `core/api/`: explicit backend response types and relative REST calls.
- `features/dashboard/`: polling/composition state, Dashboard and channel cards.
- `layout/`: persistent shell/navigation/header and a shared small placeholder.
- `src/styles.css`: local color, typography, spacing and control tokens.

`/` redirects to `/dashboard`. `/sources`, `/modulators`, `/modulators/:id`,
`/audio-profiles` and `/system` are clearly marked M7B placeholders.
Physical IDs are shown as TX 0..7, matching backend identifiers.

## Data and status

Each refresh joins GET `/api/v1/status`, `/api/v1/modulators` and `/api/v1/sources`.
The source relationship is resolved locally; no duplicate backend name field is
added. Eight fixed card positions are maintained and an invalid/incomplete physical
inventory is treated as unavailable. Missing source references remain readable.

The shell owns a five-second polling subscription. Requests do not overlap; slow
refreshes share one in-flight operation. Each refresh has a 15-second timeout.
Destroying the shell stops its timer and cancels requests. All routes retain the
same header and polling state. Failed refreshes retain previous configuration but
mark runtime values UNKNOWN/stale, including the active count and RF readback.
A successful later poll restores the display automatically.

Desired ON/OFF is labelled separately from observed TX state. Runtime nominal RF
readback null stays an em dash, even if a configured nominal target exists. This
is not measured power. PCM Flowing means consumed bytes increased between received
snapshots, not proof of sound or RF reception. ANALOG audio mode shows Audio input: Analog. No flow observed
is descriptive, not a claim that RF has stopped. Latched hardware error codes alone
are not treated as current faults; source ERROR is identified directly on the card.
Mock RP/SPI operation is explicitly identified in the shell.

PS/AF/PI/ECC are persistent configured content. Dynamic PS shows its configured
carousel entries (with a full-text tooltip); no current on-air PS item is invented.
RDS state is the backend state, not a fabricated transmission confirmation.

## Operational actions

Per-card START/STOP uses POST `/api/v1/modulators/{id}/start` or `/stop`. It is a
real button separate from the configuration navigation link. A desired-ON or
observed ON/STARTING channel offers STOP, allowing desired state to be cleared
when hardware is uncertain. START is disabled for obviously missing frequency or
missing/disabled source; hardware/frequency-band validation remains authoritative
on the backend. No inline persistent configuration fields are editable.

One UI lifecycle operation runs at a time. Pending controls prevent duplicates;
a successful POST is reported as accepted, never optimistically painted TX ON.
The prior refresh completes before the action, polling pauses during the operation,
and all relevant data refreshes immediately afterward, including on failure.
Lifecycle requests time out at 120 seconds and report an uncertain outcome rather
than retrying side-effecting requests automatically.

START ALL visits eligible physical channels sequentially through the same start
endpoint. The existing bulk start endpoint attempts unconfigured channels too, so
M7A deliberately uses per-channel requests to skip obviously ineligible channels.
Skipped IDs and partial failures are reported; backend validation still decides
whether each eligible start succeeds. STOP ALL uses POST
`/api/v1/modulators/stop-all`, which attempts all eight and returns per-ID status
or errors. Missing results are failures. A transport failure never implies all
channels stopped. Failure messages are shown both globally and on affected cards.

## Visual approach and accessibility

The interface is a neutral-dark technical panel: fixed narrow navigation, compact
persistent header, aligned channel modules, restrained borders and dominant
monospaced frequencies. Semantic green/amber/red communicate operating/warning/error
states; most values stay neutral. STOP controls use a restrained contrasting border.
The normal desktop grid is 4 x 2; medium screens use two columns and small screens
one. The navigation collapses to a keyboard-accessible Menu button below 850 px.
Buttons/links are semantic, have accessible names/focus outlines, and state is
always expressed in text. No gradients, external fonts, decorative charts or animation.

## Development and production build

Use Node 24 LTS, at least 24.15, and npm 11 or 12. The checked lockfile is generated
with Node 24.19 / npm 12.0.2. No global Angular CLI is required.

```sh
cd webui
npm ci
npm start
# http://localhost:4200
npm test
npm run typecheck
npm run build
```

`npm start` uses `proxy.conf.json`: relative `/api/**` requests forward to
`http://127.0.0.1:8080`. Run fmheadendd locally or forward a remote development API
to that local port with SSH. For another target, create an uncommitted proxy file
and run `npx ng serve --proxy-config path/to/proxy.json`; never put a development
Pi hostname in component code. No CORS/backend change is needed.

Production output is `webui/dist/fmheadend-ui/browser/`. The production host will
need SPA fallback for frontend routes and same-origin `/api` forwarding. FastAPI
static hosting and production deployment integration remain for M7B.

Windows network shares can block execution of esbuild. In that environment, copy
`webui` to a local working directory excluding `node_modules`, `.angular` and `dist`,
then run `npm ci` and the same commands there. Keep reviewed source/lockfile changes
in the repository. This was necessary for the available Windows validation host.

npm 12 reported blocked optional dependency install scripts for esbuild, lmdb,
msgpackr-extract and @parcel/watcher. The provided platform binaries worked for
validation; scripts were not globally enabled to bypass this policy. No ESLint
configuration is added: strict TypeScript and strict Angular templates provide
compile checks, and the generated Prettier dependency can check formatting.

## Manual smoke with existing backend

Use an isolated test database/runtime directory so operational configuration is not
changed. Start the native mux with `--transport mock`, then fmheadendd with
`--mock-rp`, pointing both at the same runtime directory (see M3/M6A commands).
Use the existing API to create an HTTP source with a real accessible URL and assign
an explicitly chosen frequency to at least one channel. Enable RDS if desired.
Start that channel, then run the Angular dev server and open Dashboard.

Example commands, in separate terminals (choose unused test paths and a real HTTP URL):

```sh
fmheadend-muxd --transport mock --runtime-dir /tmp/fmheadend-ui-run
fmheadendd --mock-rp --database /tmp/fmheadend-ui.db \
  --mux-runtime-dir /tmp/fmheadend-ui-run --host 127.0.0.1 --port 8080
curl -sS -X POST localhost:8080/api/v1/sources -H 'Content-Type: application/json' \
  -d '{"name":"UI smoke","type":"HTTP","config":{"uri":"https://example.org/replace-with-real-stream"}}'
# Substitute the returned source ID below.
curl -sS -X PATCH localhost:8080/api/v1/modulators/0 -H 'Content-Type: application/json' \
  -d '{"input_source_id":1,"frequency_khz":91600,"rds_enabled":true,"rds_pi":41491,"rds_ecc":224}'
# Then, from webui/:
npm start
```

Verify:

1. Eight physical cards; source name, frequency, configured PS/PI/ECC/AF match API.
2. RP/SPI mock indicators, source state, RDS state and polling timestamp are visible.
3. After two snapshots, increasing PCM consumed counters show Flowing.
4. Per-channel STOP/START and START ALL/STOP ALL update through backend responses.
   Unconfigured channels are skipped by START ALL; failed IDs are reported.
5. Stop mux: DEGRADED and unavailable PCM appear while the API and UI remain usable.
6. Stop backend: unavailable/stale appears and navigation remains usable. Restart
   backend: data recovers without page reload. Restore mux to return healthy status.
7. Inspect desktop, medium and small widths and keyboard navigation/focus.

The performed browser smoke used real Linux fmheadendd, a local HTTP WAV source,
native GStreamer and mux, with mock RP/SPI. It exercised both individual actions,
bulk actions, real mux loss, real backend stop/restart and 1440/1024/390 px widths.
A headless Chrome check drove the real buttons and inspected rendered DOM; desktop
screenshot was visually reviewed. No heavyweight E2E dependency or fixture backend
was added to the repository. No RF hardware or physical transmission was tested.

## Remaining M7B work

Sources management, modulator/RDS editing, Audio Profile management, System page
contents and production hosting/deployment integration remain unimplemented.
M6B performance/stress work,
RP firmware and unrelated features remain outside this milestone.


## Final validation

| Check | Result |
|---|---|
| Full frontend suite | 20 passed, 0 failed, 0 skipped (2 test files) |
| Production Angular build | Passed; 289.51 kB initial raw / 79.70 kB estimated transfer |
| Strict TypeScript check | Passed |
| Strict Angular template compilation | Passed in production build |
| Prettier source check | Passed |
| Real-backend browser smoke | Passed, as described above |

The production build emitted no warnings. npm installation warnings and the
network-share esbuild limitation are documented above. The test/build ran from
a clean local temporary copy with `npm ci`; source and lockfile remain in `webui/`.
The original M7A pass changed no backend files. Focused backend/API tests
were subsequently run for the Modulator identity correction documented below.
Smoke processes/tunnels were stopped and the isolated Pi test database was removed
by its temporary-directory cleanup. All project changes are uncommitted. No
architectural deviation or M7B functionality was introduced.


## Modulator identity correction

Physical channel ID 0..7 is the only Modulator identifier. The backend configuration,
response and PATCH schemas and frontend types/cards have no `name` field. PATCH
requests containing `name` are rejected as unknown fields. Source and Audio Profile
names remain unchanged.

Fresh schema-1 databases store physical Modulator IDs without a name field. No replacement naming field is introduced. Old development databases are unsupported and may be recreated; see architecture section 16.4.


Historical correction validation covered backend identity/API behavior and 20 frontend Dashboard/API tests, with TypeScript checking. The unreleased migration tests have since been removed; current database coverage tests direct schema-1 initialization, reopen, version rejection, and integrity.
