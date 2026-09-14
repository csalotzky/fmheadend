# Milestone 7B5 - System, region plans and production WebUI

## V1 surface

Sitemap: Dashboard, Sources (list/new/editor), Modulators (list/editor IDs 0..7),
Audio Profiles (list/new/editor), System. No unfinished navigation placeholders.
Bandscan is not a V1 feature. Global START ALL / STOP ALL stay in the shell;
System does not duplicate controls or offer OS/service/database administration.

System is read-only and uses `/api/v1/status` and `/api/v1/system/info`.
It shows backend version/status and selected media backend; RP state/protocol/
firmware/hardware; mux state/transport/frame count; schema and frequency plan.
Diagnostics show existing I2C/SPI/audio-link counters and current errors. Historical
RP codes are labeled as history, never used alone as active fault indicators.
Unknown dependency information stays unavailable, and one failed subsystem does
not replace the page with an error screen. No fabricated CPU/temperature/plugin
health, live logs, raw JSON, register UI or shell console is added.

Info refresh follows existing shell status snapshots (normally five seconds),
with cancellation/cleanup and separate failure handling. Modulator editor loads
its plan from system info with its configuration. Failed info requests never
silently substitute Europe. Shared centered page/header/editor-action conventions
apply; the Dashboard retains its operational eight-channel layout.

## Startup region

`--region 1` is the default, including when omitted. Unknown values fail argument
parsing before database/server startup. The obsolete custom min/max CLI options
are removed. The sole plan table lives in `daemon/regions.py`.

| Code | Name | Band MHz | Raster |
|---|---|---|---|
| 0 | Expert / full hardware range | 76.0-108.0 | 50 kHz |
| 1 | Europe / standard | 87.5-108.0 | 100 kHz |
| 2 | United States | 88.1-107.9 | 200 kHz |
| 3 | Japan | 76.1-98.9 | 100 kHz |

Region only controls main Modulator frequency choices. It does not change
pre-emphasis, RDS/PTY/AF, attenuation, profiles, stereo, QN gain or persisted
transmitter configuration. Hardware validation independently accepts 76000..108000
kHz on a 50 kHz raster. Changing region never retunes or rewrites a TX.

The editor derives options using integer kHz addition from the backend plan.
A stored out-of-plan frequency appears explicitly labeled and stays unchanged
unless the user selects another. Null remains a required selection with no default.
RDS AF retains its independent 87600..107900 kHz / 100 kHz Method A options.

## Production hosting

Node is needed to build, not to run the deployed UI. With Node/npm installed as
documented by `webui/package.json`, from the repository root:

```sh
cd webui
npm ci
npm run build
cd ..
```

Browser assets are in `webui/dist/fmheadend-ui/browser`. Copy that directory's
contents to the administrator-selected deployment location, for example
`/opt/fmheadend/webui`. Example deployment command (administrator paths must be installed separately):

```sh
fmheadendd --region 1 --host 0.0.0.0 --port 8080   --webui-dir /opt/fmheadend/webui --mock-rp   --database /var/lib/fmheadend/fmheadend.db
```

This example uses mock RP but retains native GStreamer and independent muxd;
start muxd separately as in M6A. Open `http://HOST:8080/`. The same arguments can
be supplied through the existing `FMHEADEND_ARGS` EnvironmentFile. Choose paths
and hardware/mock options for the installation; no development host is hard-coded.
Without `--webui-dir`, the daemon remains API-only. A configured directory must
contain `index.html`. Static assets and known client routes are served by FastAPI;
client routes get index fallback. Unknown `/api/...` stays JSON 404, never HTML.
Missing assets and unrecognized routes stay 404. No production Node server is used.

For development, `cd webui && npm start` keeps the existing relative `/api` proxy
workflow. Production hosting does not change it. System region is read-only;
edit startup arguments and restart the daemon to select another plan.

## Validation

Focused backend: `python -m pytest daemon/tests/test_region_system.py daemon/tests/test_backend.py -q`.
Frontend: `npm test`; production: `npm run build`. No RF or hardware bring-up,
performance benchmark, OS administration or new audio/RDS feature is included.

Validation result: 53 focused backend tests passed; 137 frontend tests passed;
production build passed (426.32 kB initial raw bundle, 106.84 kB estimated transfer).
A real Pi daemon served these built assets with `--mock --region 2`, a temporary
DB, temporary asset directory and a free loopback port. Six direct client routes,
a JavaScript asset, current region info and JSON API 404s passed. Processes,
assets and DB were cleaned up. This validates the serving/CLI mechanism; no
system-wide `/opt` installation or physical RP/RF test was performed. The existing
backend suite emitted two dependency deprecation warnings, with no failures.

## Database format

System diagnostics report database schema version 1, the current pre-V1 format.
Startup initializes an empty database directly, opens current version 1 without
rewriting saved configuration, and rejects any other nonzero version with
instructions to recreate the development database. No released database format
exists yet. See architecture section 16.4 for the pre-release reset and future
released-format migration policy.
