# Milestone 6A: lifecycle recovery and systemd

M6A adds integration recovery, safe lifecycle handling and service units. It does
not tune buffers, benchmark the Zero 2 W, implement firmware or add RDS features.
The architecture and RP protocol remain the canonical component/wire contracts.

## Startup and RP recovery

The existing single control executor owns recovery as well as API control, SQLite,
status polling and RDS refill. No new I2C worker, supervisor or process launcher
is introduced. Startup and reconnect use the same `Recovery` implementation.

After loading configuration, startup attempts GET_INFO and checks protocol V1 and
exactly eight modulators. Missing/incompatible RP hardware leaves the API available
with DEGRADED status, without a mock fallback. Failed attempts back off for two
seconds (in addition to the low-level client's existing bounded transaction policy).
Normal status polling drives recovery; RDS refill never performs its own reconnect.

A successful handshake is followed by:

1. Disable all eight physical transmitters before restoring any desired ON state.
2. Apply SET_TX_CONFIG to each configured channel. Unconfigured channels stay OFF;
   no frequency or source mode is invented for an incomplete configuration.
3. RDS_CLEAR each FIFO and discard/reset its Linux pending/scheduler boundary.
4. Retain or acquire the desired audio sources, then enable only desired ON channels.
5. Read GET_STATUS and require the reported TX states to match desired state before
   declaring resynchronization complete or allowing normal RDS refill.

Invalid desired ON configuration or a failed operation leaves recovery incomplete
and DEGRADED. Persistent configuration is never rewritten to represent temporary
OFF/unknown states. API start/config/stop operations during RP unavailability retain
validated desired changes but reject hardware application until recovery succeeds.

RP communication loss closes the real session, invalidates hardware observations
and suspends RDS. Source pipelines/PCM may keep running. RDS errors also invalidate
the recovery boundary; pending batches are discarded rather than replayed after an
ambiguous outcome. After reconnect, effective RT is selected by the unchanged M5
precedence and PS/AF/RT/ECC restart coherently. RP uptime decreasing between successful
status observations is also treated as a reset. V1 has no boot identifier: a reset
whose uptime overtakes the prior observation before the next poll cannot always be
detected if no communication failure was observed.

Current hardware fault observations remain distinct from latched diagnostic error
codes: historical counters/codes alone do not mean the RP is currently unhealthy.
No RF measurement or successful physical RDS transmission is inferred.

## Mux and source independence

Mux control status reconnects on each existing bounded status query. PCM writer
connections retry independently, including when mux was absent at initial attach.
Existing bounded queues and stale-audio discard behavior are unchanged. On mux
return, producers resume without API reconfiguration, RF reconfiguration, or a
new GStreamer pipeline. `fmheadendd` never starts or kills muxd.

An existing HTTP source failure/reconnect does not imply an RP failure. TX may
remain ON while the mux emits silence. M2 reconnect/backoff remains responsible
for restarting ingest. M6A only changes source shutdown cleanup so one failed
pipeline teardown does not prevent attempting the remaining pipelines.

## Shutdown and status

Uvicorn handles SIGTERM through the ASGI lifespan. Shutdown cancels maintenance,
suspends/discards RDS work, attempts RF disable, releases producers/source workers,
closes mux and RP resources, and closes SQLite/control execution. It does not save
`enabled=false`. On RP communication failure, the shutdown disable pass stops rather
than hammering unavailable hardware; OFF cannot be guaranteed without communication.
The existing I2C adapter timeout and worker join bounds apply. systemd's 30-second
TimeoutStopSec is the final bound if a native driver/worker does not return.

The existing `/api/v1/status` remains the health surface. Failed dependencies yield
DEGRADED while HTTP remains available; successful current dependency observations
and restoration return RUNNING. On RP loss, hardware TX, nominal RF level and RP
counters become null, TX runtime state becomes ERROR, and RDS becomes UNKNOWN.
Desired enabled state is still read from the persistent modulator configuration.
Mux counters are last-observed diagnostics while mux_state=ERROR, not proof of
current transport. No second health API is added.

Standard application logging writes to stdout/stderr for journald. Startup,
shutdown, RP compatibility/loss, resynchronization and mux state transitions are
logged. Repeated identical dependency failures are not logged on every poll.

## systemd deployment

Units are in `systemd/`. They are independent; fmheadendd has Wants/After (not
Requires/BindsTo) for muxd so stopping/restarting mux does not stop the HTTP service.
Mux owns `/run/fmheadend` via RuntimeDirectory. StateDirectory creates
`/var/lib/fmheadend`. Both run as the existing dedicated `fmheadend` user/group.
Grant that account access to `spi`, `i2c` and `audio` groups as appropriate for the
Pi. These groups must exist; units do not install drivers or create accounts.

Documented executable locations are `/usr/local/bin/fmheadend-muxd` and
`/opt/fmheadend/venv/bin/fmheadendd`. Build/install using the existing M3/backend
instructions, or use `systemctl edit` to override ExecStart for an existing checkout
and virtualenv. An ExecStart override must first clear the old ExecStart. There is
no installer or package-management framework in this milestone.

Create `/etc/fmheadend/muxd.env` explicitly. For hardware-independent operation:

```ini
MUXD_ARGS="--transport mock --runtime-dir /run/fmheadend"
```

For actual SPI bring-up, explicitly select `--transport spidev --spi-device
/dev/spidev0.0 --spi-clock-hz 25000000` instead and follow M3 spidev prerequisites.
Mock transport is never selected as a fallback.

Create `/etc/fmheadend/fmheadendd.env` for current hardware-free testing:

```ini
FMHEADEND_ARGS="--database /var/lib/fmheadend/fmheadend.db --mock-rp"
```

For real RP, remove `--mock-rp` and configure `--i2c-device /dev/i2c-1` and the
actual `--rp-i2c-address` (or set those persistent system fields). No slave address
is invented. Environment files are required so transport/hardware mode is explicit.

Once executable paths, account/groups and environment files are configured:

```sh
sudo cp systemd/fmheadend-muxd.service systemd/fmheadendd.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/fmheadend-muxd.service /etc/systemd/system/fmheadendd.service
sudo systemctl daemon-reload
sudo systemctl enable --now fmheadend-muxd.service fmheadendd.service
journalctl -u fmheadendd -u fmheadend-muxd -f
```

Do not run a second daemon against the same database/RP/runtime sockets. No units
are enabled or installed automatically by development tests.

## Manual smoke now: real HTTP and native mux, mock RP/SPI

Use the service setup above, or run the two executables separately with mock SPI
and `--mock-rp`. Create an HTTP source with a working HTTP/HLS URI:

```sh
curl -sS -X POST localhost:8080/api/v1/sources -H 'Content-Type: application/json' \
  -d '{"name":"smoke","type":"HTTP","config":{"uri":"https://example.org/replace-with-live-stream"}}'
# Substitute the returned source ID and explicit local test frequency below.
curl -sS -X PATCH localhost:8080/api/v1/modulators/0 -H 'Content-Type: application/json' \
  -d '{"input_source_id":1,"frequency_khz":98100,"rds_enabled":true,"rds_pi":4660,"rds_ecc":224,"rds_rt":"Recovery smoke"}'
curl -sS -X POST localhost:8080/api/v1/modulators/0/start
curl -sS localhost:8080/api/v1/status
```

Compare snapshots: source RUNNING, increasing PCM received/consumed and RDS accepted
counters, and mock flags must be visible. Stop mux long enough to observe DEGRADED,
then start it again (for a crash test, SIGKILL the mux MainPID; Restart=on-failure
will bring it back automatically):

```sh
sudo systemctl stop fmheadend-muxd
curl -sS localhost:8080/api/v1/status
sudo systemctl start fmheadend-muxd
curl -sS localhost:8080/api/v1/status
```

Allow the configured status-poll interval. Confirm fmheadendd stayed alive, desired
ON was preserved, status returns RUNNING and PCM consumed increases from the new
mux process counters. No source/modulator reconfiguration is needed. Stop/start
fmheadendd as well and verify desired ON restores through the same safe path.

The automated `test_real_http_gstreamer_to_mux_via_existing_api[True]` performs the
kill/restart variant with a local HTTP WAV server, native GStreamer, real mux Unix
sockets and mock RP/SPI. It checks resumed PCM, unchanged pipeline identity and
DEGRADED -> RUNNING. This is an integration smoke, not a stress or soak test.

## Future real RP smoke

Use compatible Pico firmware and an appropriate RF test setup. Configure one TX
explicitly, observe normal status, then disconnect/reboot RP. Check DEGRADED,
unknown hardware fields, preserved desired ON and suspended RDS. Reconnect and
inspect journal/firmware diagnostics for GET_INFO, all-channel disable, config,
RDS_CLEAR, desired enable and successful read-back. Verify PS/AF/ECC/RT clean restart
with receiver/firmware instrumentation. A failed config/clear must not enable that
TX or claim recovery. Finally SIGTERM fmheadendd and verify RF disables while the
saved desired state remains ON. No real RP recovery or physical RF/RDS transmission
has been tested in M6A without connected hardware.

## Validation and remaining scope

Focused tests: `python -m pytest daemon/tests/test_recovery.py -q -ra`.
For the real-process smoke and full regression use the M3/M5 `MUXD_BINARY` and
`FMHEADEND_TEST_ALSA_DEVICE` settings. Native mux regression remains CTest.
Systemd validation checks unit structure and `systemd-analyze verify` with actual
available executable paths; it does not install services or simulate a systemd boot.
No new dependencies are required.

M6B remains responsible for Zero 2 W performance/stress/soak testing, measurements,
resource optimization and any evidence-based buffer tuning. Firmware, UI and all
other later features remain outside M6A.


## Recorded validation

| Suite | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Pi focused M6A recovery | 11 | 0 | 0 |
| Pi recovery + mux client/native integration | 27 | 0 | 0 |
| Pi complete Python regression | 307 | 0 | 0 |
| Native mux CTest | 8 | 0 | 0 |
| Windows complete Python regression | 286 | 0 | 21 |

Focused counts are subsets of the full suite, not additional coverage totals.
Windows skips 15 GStreamer cases, five Linux mux cases and one real Linux
missing-I2C-device case. Both Python environments report two existing dependency
deprecation warnings. Complete Pi Python and CTest were each run once near the end;
the focused recovery suite was rerun after final error-normalization hardening.

`systemd-analyze verify --man=no` exited 0 on the Pi after substituting the existing
venv/native mux executable paths in temporary copies of the units. This validates
unit syntax/dependencies without installing units or pretending production paths
already exist. No systemd service was enabled or restarted by these tests.

RP recovery tests use the existing MockRp2354Client; protocol regression mocks
low-level OS I2C calls. The missing-I2C-path test uses real Linux OS calls.
The mux restart smoke uses a real Linux C++ process (SIGKILL/restart), real HTTP
server/GStreamer and Unix PCM/control sockets, with mock RP and mock SPI. PCM resumes
on the same source pipeline without reconfiguration. No physical RP/QN hardware
was used and physical RF/RDS transmission or recovery remains unverified.

The old architecture Requires= example was changed to Wants=/After= as explicitly
requested for M6A, so mux failure cannot stop the HTTP service. No component-boundary
redesign, extra worker architecture, new dependency or M6B work was introduced.
