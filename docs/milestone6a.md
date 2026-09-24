# Milestone 6A: lifecycle recovery and systemd

M6A adds integration recovery, safe lifecycle handling and service units. It does
not tune buffers, benchmark the Zero 2 W, implement firmware or add RDS features.
The architecture and RP protocol remain the canonical component/wire contracts.

## Startup and RP recovery

The existing single control executor owns recovery as well as API control, SQLite,
status polling and RDS refill. No new I2C worker, supervisor or process launcher
is introduced. Recovery scope must not exceed failure scope. Reopening the Linux
I2C session does not imply loss of RP/QN state.

After loading configuration, startup attempts GET_INFO and checks protocol V1 and
exactly eight modulators. Missing/incompatible RP hardware leaves the API available
with DEGRADED status, without a mock fallback. Transport failures use the shared
250 ms client/coordinator backoff; normal later polls retry communication. No
additional fixed one-second RDS transport delay is imposed.

At application startup only, a successful handshake is followed by:

1. Disable all eight physical transmitters before restoring any desired ON state.
2. Apply SET_TX_CONFIG to each configured channel. Unconfigured channels stay OFF;
   no frequency or source mode is invented for an incomplete configuration.
3. RDS_CLEAR each FIFO and discard/reset its Linux pending/scheduler boundary.
4. Retain or acquire the desired audio sources, then enable only desired ON channels.
5. Read GET_STATUS and require the reported TX states to match desired state before
   declaring resynchronization complete or allowing normal RDS refill.

Invalid desired ON configuration or a failed operation leaves recovery incomplete
and DEGRADED. Persistent configuration is never rewritten to represent temporary
OFF/unknown states. API START/STOP saves validated desired changes even while
startup synchronization is incomplete; hardware application waits for startup to
complete. Communication loss after startup does not close this readiness gate.

Startup retains progress through acknowledged steps: a corrupt final status read
retries observation, not all eight OFF/config operations. It records each positively
acknowledged startup enable state. If START/STOP changes desired state before the
final observation succeeds, only targets with obsolete confirmed states receive
another desired-state enable write before the next observation. Unrelated completed
OFF/config/CLEAR/enable steps are retained. There is currently no
daemon-issued RP reset path. Startup is the only full hardware synchronization
trigger. A suspicious uptime decrease (including wrap), status mismatch, failed poll
or reconnect never invokes global restoration. Valid hardware faults/mismatches
remain visible; they are not silently converted to success or configuration replay.

The client preserves command, target, transaction stage and execution certainty:
NOT_SENT, AMBIGUOUS, read-only OBSERVATION failure, or a valid RP RESPONSE. An
implicit GET_INFO failure is not an executed SET. Local validation raises ValueError
before I/O. Valid QN_ERROR/INTERNAL_ERROR remain typed RP errors.

Read-only GET_INFO/GET_STATUS/GET_RDS_FIFO_FREE failures perform no RF/config/CLEAR
recovery. Capacity failure skips refill and preserves scheduler/pending state; a
long outage may starve RDS but leaves RF/audio untouched. A new session performs
GET_INFO compatibility checking without replaying configuration. Existing locks
still serialize request, command wait, header read and payload read.

An ambiguous RDS_PUSH discards only that TX's uncertain pending batch and fences
its FIFO until RDS_CLEAR is positively acknowledged. An uncertain CLEAR is retried
without intervening PUSHes. Recovery then refills to the unchanged two-second
lookahead in that same tick, bypassing the normal 100 ms gate. NOT_SENT retains
the pending batch; BUSY retains its existing exact-batch/single-retry semantics.
Valid RP RDS error replies retain a bounded channel retry interval. Other channels
continue once the shared bus is usable; no RDS failure disables RF.

An ambiguous SET_TX_ENABLED queues just the target's latest desired ON/OFF write,
without an unnecessary OFF/config cycle. An ambiguous SET_TX_CONFIG queues a
target-only OFF/config/CLEAR/desired-state reconciliation. At most one target
lifecycle runs per maintenance tick, before RDS refill. Completed steps are not
repeated merely because the next ACK is lost. NOT_SENT or explicit SET rejection
does not create reconciliation work. Persistent desired state and STOP ALL holds
remain authoritative. Explicit OFF still releases audio only after confirmation.

### Explicit START/STOP under a transient session failure

START validates and saves desired ON, prepares the DIGITAL source/mux, then sends
SET_TX_CONFIG, the RDS_CLEAR boundary, and SET_TX_ENABLED(true), in that order.
STOP saves desired OFF, sends SET_TX_ENABLED(false), then stops RDS and attempts
mux/source cleanup. Neither lifecycle adds a synchronous GET_STATUS. Periodic
status remains observation; a later failed read cannot revoke a confirmed command.

Each explicit lifecycle request has at most **one extra session reacquisition**,
shared across its RP commands. START ALL/STOP ALL share that one budget across all
eight targets and retain their existing partial-result/persistent-state semantics.
The allowance requires NOT_SENT metadata identifying device/session open, active
backoff, or an implicit GET_INFO failure before the requested command. It waits
out the existing shared backoff (at most 250 ms); a longer hold returns failure
without bypassing it. The second attempt's failure propagates normally. Existing
bounded command waits and the client's BUSY retry remain unchanged.

There is no lifecycle retry for a write that may have begun, a missing/corrupt SET
response, an explicit RP rejection, incompatibility, validation, or source failure.
There is no new background retry queue. Ambiguous SETs retain target-only recovery;
NOT_SENT failures retain desired state without scheduling reconciliation. HTTP 503
messages distinguish `not sent` from `outcome unknown (AMBIGUOUS)` and explicit RP
errors. Shutdown does not use this additional retry allowance.

After a valid OFF ACK, HTTP STOP returns 200 with `tx_state=OFF`, including when
mux/source teardown subsequently fails. Cleanup continues for the other resource;
failed source release retains ownership, preventing duplicate pipelines. Existing
`last_error` reports `RF OFF confirmed; cleanup pending: ...`, is logged, and makes
the status DEGRADED without marking the TX ERROR. A later STOP retries cleanup;
START cannot proceed past a failed cleanup of a retained source. Failed post-OFF
RDS_CLEAR remains a target-local fence/`rds_last_error`, not an RF failure.

These behaviors are covered by deterministic client/HTTP/manager tests, including
CRC corruption, exhausted reacquisition, ambiguous SET after reacquisition,
cleanup failures and a desired OFF following failed startup observation. Hardware
validation under real I2C corruption remains an operator test.

Validation for this hardening pass: focused lifecycle/RP/recovery/RDS suites
390 passed, 1 skipped; full daemon suite 558 passed, 30 skipped. Skips require
Linux I2C/mux or GStreamer dependencies unavailable in the Windows test environment.

Current hardware fault observations remain distinct from latched diagnostic error
codes: historical counters/codes alone do not mean the RP is currently unhealthy.
No RF measurement or successful physical RDS transmission is inferred.

## Editor save-and-resume

Plain PATCH remains persistence-only. Editing requires confirmed runtime OFF,
released source ownership, completed startup and no target reconciliation pending.
The old `enabled=true` guard incorrectly rejected successful STOP ALL edits:
desired enabled is saved startup/START ALL intent, while the session operational
hold keeps that target stopped. Neither PATCH nor stopped editor Save clears it.

`POST /api/v1/modulators/{id}/save-and-resume` accepts the existing ModulatorPatch.
It runs entirely within one existing serialized control-executor call:

1. Validate the complete merged configuration without side effects.
2. Determine resume intent from runtime ON, desired ON and absence of a hold.
3. For that running target only, set a temporary operational hold and call the
   existing internal STOP (no persistent `enabled=false` write).
4. After confirmed OFF and successful cleanup, persist the edits.
5. Release only the operation's temporary hold and call the existing START using
   the saved configuration. Share one bounded NOT_SENT retry budget across stages.

An already-stopped target only saves. A held but still ON/uncertain target is
rejected; Save never uses a failed STOP ALL as permission to edit or restart.
No synchronous GET_STATUS, parallel worker, RDS policy or wire change is added.

Pre-operation validation/readiness conflicts use the existing 4xx/503 responses.
Once orchestration begins, HTTP 200 carries its **result**, not a guarantee that
every stage succeeded: `saved`, `stop_confirmed`, `restarted`, `failed_stage`
(`stop`, `save`, `start`, or null), `message`, and `configuration` (null until saved).
`stop_confirmed` records the confirmed OFF boundary, not final RF state after restart.

- Failed/ambiguous STOP: no SAVE/START. Keep edits unsaved and the temporary hold;
  target-only reconciliation may establish OFF but cannot inadvertently re-enable
  the previous desired-ON state. Do not replay the uncertain command in the request.
- STOP succeeds, SAVE/cleanup fails: retain old persistent configuration/desired
  state and keep the target stopped under the hold. Explicit Start/Start All can
  later resume; there is no automatic compensating restart.
- SAVE succeeds, START fails: return the saved configuration and an explicit
  restart failure. Existing execution-certainty and target-only reconciliation
  apply to any ambiguous START command; there is no request-level blind replay.
- Success: return the saved configuration and whether RF was resumed.

The frontend submits one request, prevents duplicate clicks/navigation while it
is pending, and does not reload dirty edits from polling. Unsaved failures retain
the form; saved-but-restart-failed results update its saved baseline and show the
restart error beside Save. HTTP transport loss means the aggregate result is not
known; it does not authorize automatic replay. Shutdown/STOP ALL semantics remain.

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
and restoration return RUNNING. On communication failure the last valid hardware
TX, attenuation and counter values are retained. `rp_communication_state` reports
AVAILABLE/UNAVAILABLE/INCOMPATIBLE; `rp_observation_stale` marks uncertain data and
`rp_observation_age_s` measures time since the last successful snapshot (null before
one exists). Transport uncertainty does not turn every TX into hardware ERROR.
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

## Real RP scoped-recovery smoke (operator validation pending)

With the terminated RF bench setup, observe running TXs during the intermittent
host-I2C corruption. Failed polls should mark observations stale/DEGRADED without
OFF/config/CLEAR writes. After a good read the observation becomes fresh. An
uncertain RDS_PUSH should produce only that TX's CLEAR/refill, without RF change.
Confirm other TX audio and RDS continue after the shared transport backoff.

An uncertain enable ACK should repeat only the target desired enable value; an
uncertain config ACK may reconcile only that TX. Inspect both Linux logs and RP
UART. Do not equate unplugging/rebooting the RP with a control-only outage: actual
reset loses hardware state, and V1 currently provides no reliable automatic reset
identification here. Restarting the daemon explicitly performs startup restore.
Finally SIGTERM fmheadendd and verify the bounded OFF pass, preserved persistent
desired state and an explicit warning if an OFF cannot be confirmed. These Linux
recovery changes have automated coverage, not final physical acceptance yet.

## Validation and remaining scope

Scoped-recovery update validation (Windows, isolated Python 3.13 test dependencies):
focused recovery/RDS/RP-client suites **271 passed, 1 skipped**; full daemon suite
**474 passed, 30 skipped**. Skips require Linux i2c-dev, native Linux mux integration,
or GStreamer/PyGObject. The existing transaction-serialization and two-second RDS
lookahead tests pass. No firmware, wire format, SPI/audio code or Linux scheduler
policy was changed. Physical fault/recovery acceptance remains for the operator.

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

### Production V1 WebUI startup arguments

The existing `FMHEADEND_ARGS` environment entry can include `--region 1` and
`--webui-dir /opt/fmheadend/webui`. Build/install the browser assets separately;
no Node server or extra systemd service is needed. Region 1 is the default.
See [M7B5](milestone7b5.md) for the production hosting procedure.

## File Audio Profile saves (schema 2 integration)

Profile PUT shares the existing control executor. After validation and atomic save,
it restarts only active Sources selecting that file, preserving consumer sets and
PCM output objects. It never invokes ModulatorManager START/STOP, changes desired
state/STOP ALL holds, or calls QN/RDS control. Source teardown failure retains
ownership; saved-file/restart failure is a partial outcome, not failed RF control.
Source configuration edits retain their existing stop-before-edit guard.
See [file profiles](audio-profile-files.md) for migration and API response details.
