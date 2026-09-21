# M8.6B: production RDS FIFO consumption and polling handoff

## Startup and timeout hardening (physical validation pending)

Subsequent operator testing physically established working M8.6B over-air RDS,
high-byte-first words, toggle/poll handoff, sustained approximately 11.4 groups/s,
FIFO refill, starvation and refill recovery without RF restart. A prefilled FIFO
allowed a first handoff about 0.5 ms after TX ON and that attempt timed out. The
later empty-FIFO 0/0 tool run worked with first PUSH after the existing roughly
150 ms SET_TX_ENABLED host interval. These are measured findings; they do not
prove arbitrary timeout cancellation or a maximum late acknowledgement latency.

The current uncommitted follow-up implements a **nonblocking 150 ms per-TX RDS
startup gate**, recorded after observing/verifying TXREQ ON. OFF clears it;
OFF->ON, configuration restoring RF and an observed RDS-enable transition while
ON re-arm it. Repeated ON does not postpone it. The consumer performs no RDS
data/handoff work before expiry; queued data remains intact. There is no new
sleep or Linux startup delay. SPI/I2S, host requests and other channels continue.

The default `rds_tx_smoke.py` run again queues 23 groups before ON. Capture the
first firmware `submitted` UART line and verify **`tx_age_us >= 150000`**. This
age is from the verified/observed enable timestamp, conservatively later than
the actual register write, and is not measured by the Pi clock. Status alone
cannot prove this interval. To reproduce the earlier empty-FIFO A/B diagnostic,
add `--empty-until-feed`; a nonzero `--post-tx-settle-ms` requires that option.

Production build and all **20 native/tool suites pass** with the startup gate
and timeout recovery (including the existing tool tests).
Tests include the prefilled boundary, nonblocking behavior, independent channels,
CLEAR during the gate, repeated ON, stop/start and reconfiguration. Physical
acceptance of this new gate remains pending.

**ACK timeout recovery is now implemented as explicitly requested.** The existing
500 ms timeout discards the original FIFO head only if its CLEAR epoch still
matches; it never counts that loss as acceptance or replays the group. It does
not set QN_ERROR, change RF/audio eligibility or increment the underrun counter.
The consumer keeps an in-flight fence while it reads SYSTEM1, clears only RDSEN,
verifies the readback, restores the original SYSTEM1 and verifies it again.
TXREQ and all unrelated bits are preserved. These five bounded bus operations
stay together in foreground execution so host status cannot observe temporary
RDS OFF and invalidate the audio source. IRQs, SPI and core1 audio remain running.

On a later service visit, STATUS2 supplies a fresh baseline. A **20 ms per-TX
nonblocking backoff** follows; IDLE then samples STATUS2 again before preparing
the next queued group. CLEAR during reset/backoff discards waiting data without
skipping recovery or accidentally popping a new FIFO generation. An actual bus
failure or SYSTEM1 readback mismatch during reset/resync uses the existing CEN
isolation and QN_ERROR path for that channel. Other channels continue.

The internal saturating `timeouts` counter is separate from `accepted` and
`rds_underrun_count`. UART reports `ACK timeout; group discarded; RF/audio retained`
and `recovery/resync; RDSEN restored; backoff 20 ms` for the first three timeouts
and every 64th thereafter. Real hardware failures retain a distinct `QN FAULT`
line. Protocol V1 and all wire fields remain unchanged.

The RDSEN reset and 20 ms backoff are **operator-authorized recovery design
choices, not physically validated cancellation guarantees or datasheet timing
requirements**. Native tests cover loss/resume, register preservation, fresh
baseline, CLEAR, repeated timeouts and bus failures at all recovery steps.
Updated firmware still needs the production-prefill physical smoke below; no
new diagnostic tool or artificial timeout injection was added. The earlier
standalone reset observer is historical and is not this production test.

Firmware **0.8.6b**, host address **0x62**, protocol **V1**. The path is unchanged:

Linux M5 scheduler -> RDS_PUSH -> eight independent 64-group RP FIFOs ->
foreground QN polling handoff -> QN8007B encoder/modulator.

The RP never examines group type/content, generates words, schedules PS/RT,
reorders or repeats groups. No firmware carousel, GPIO26 access, QN interrupt
enable, interrupt wake hint or fallback exists. Boot remains RF OFF; continuous
48 kHz I2S, the 4100-byte SPI format and QN RF/configuration operations are retained.

## Evidence and limits

### Physically established before this milestone

The operator's raw/local probes established:

- RDSD0..RDSD7 writes alone did not change STATUS2.RDS_RXTXUPD.
- Each SYSTEM2.RDSTXRDY toggle was followed by a STATUS2 bit-7 transition.
- The sustained **high-byte-first** local test decoded PS `" R D S  "`, with
  RDS indication, about **11.4 accepted groups/s**, and about **88 ms maximum
  observed acknowledgement**. TX0 remained ON/OK and timed RF OFF succeeded.
- GPIO26 was not required.

The earlier, unavailable bench attempt was **not** a known-good handshake.
The subsequent successful probes establish the polling mechanism, not a complete
production FIFO consumer. See [probe record](qn-rds-probe.md).

Neither diagnostic 300 ms settling interval was individually proven necessary.
Production adds **neither delay**. The existing M8.3 configuration sequence and
its existing 20 ms recalibration wait are unchanged. The 88 ms observation is not
a datasheet maximum or a promise about all chips, temperatures or lifecycle states.

### Implemented and tested natively, not yet proven on the board

The FIFO consumer, CLEAR races, failure fencing, lifecycle behavior, eight-channel
service load and Linux buffering described below have deterministic tests.
They do not establish RF reception or real eight-channel timing. **Final M8.6B
over-air/full-stack physical validation remains an operator task.**

## Register sequence and state machine

Eligibility: QN passed boot, remains ready/configured, TX enabled, SYSTEM1.RDSEN
(bit 1) set, and no active QN control fault. A transport fault blocks new submissions.
The normal SET_TX_CONFIG/SET_TX_ENABLED lifecycle owns RDSEN and RF; the consumer
does not change either bit or flap the carrier.

Normally, one channel visit executes one bounded QN transaction; the short
RDSEN reset sequence is the exception described above:

1. **IDLE:** peek the FIFO head under a short core0 IRQ mask, copy its four u16
   words and epoch, then read **STATUS2 0x1B** as this submission's baseline.
2. **DATA:** write **0x10..0x17** individually, in ascending register order:
   `A_hi A_lo B_hi B_lo C_hi C_lo D_hi D_lo`. This preserves the information
   words; RDS_PUSH still carries little-endian u16 words over host I2C.
3. **SYSTEM2:** read **0x01**. Refuse asserted SWRST/RECAL (`0xC0`) rather than
   echoing those control bits into an unintended operation.
4. **HANDOFF:** reserve this handoff against CLEAR under a short IRQ mask, then
   write `SYSTEM2_before XOR 0x04`. **Only RDSTXRDY (bit 2) changes.** No
   read/modify/write of unrelated RF, audio, gain or interrupt registers is added.
5. **WAIT:** read STATUS2 at the service cadence. Acceptance is exactly
   `((STATUS2_now ^ STATUS2_baseline) & 0x80) != 0`, in either direction.
   Other-bit changes, elapsed time or successful data writes are not acceptance.
6. On acceptance, pop **exactly one** head under a short mask, only if its FIFO
   epoch still matches. Clear the in-flight fence and return to IDLE. No second
   submission can occur until this boundary is resolved.
7. **RESET / RESYNC / BACKOFF:** on ACK timeout, discard the group and perform
   the recovery above before another submission.
8. **FAULT:** real bus/control ambiguity stops new submissions. See recovery below.

Register locations/bit names correspond to QN8007B/8007LB Rev.2.09 section 7,
SYSTEM1/2 (pp.26-27), RDSD0..7 (p.36), STATUS2 (p.38). The ambiguous host-buffer
wording on pp.40-41 is not used to invent an RDSD7 acceptance rule: the implemented
toggle interpretation and byte order follow the successful physical observations.

## Timing, ownership and load

- Core0 foreground serializes all QN control and RDS transactions. Host commands
  run before RDS service. There is **no QN work or UART printing in the host IRQ**.
- WAIT samples are scheduled **10 ms after the previous read**; service scheduling
  and host work can delay an actual sample. There are no wait loops or settling sleeps.
- Handoff timeout is **500 ms** per active observation window, a conservative software
  allowance exceeding the observed 88 ms by over five times. It is not a hardware
  specification. An actual STATUS2 read is performed before declaring a timeout;
  a positive transition observed after a slow foreground command remains valid.
- One service call visits at most eight channels in round-robin order, stopping
  after **3 ms** elapsed work. Ordinarily it can overrun by one bounded QN
  transaction (approximately 10 ms plus STOP cleanup). Recovery groups five
  transfers: approximately 2.1 ms healthy, or up to four successful transfers
  plus one failing bounded transfer before isolation. There are no retries in
  that sequence. The cursor resumes
  on the next channel; a failed QN cannot monopolize the bus. All IRQs remain enabled
  during bit-banging. Existing host response waits remain unchanged.
- Audio/SPI remain on the existing PIO/DMA/core1 path. No new DMA, PIO, core, thread
  or allocation is introduced. A shared physical bus fault can naturally affect
  several QNs; software isolation cannot fix an electrically stuck shared bus.
- FIFO storage remains **64 x 8 x 8 = 4096 data bytes**. Metadata including a
  CLEAR epoch/alignment makes the eight FIFOs **4192 bytes**. The consumer is
  **856 bytes** with the current native ABI, plus QN channel lifecycle/error fields.
- IRQ owns PUSH/CLEAR; foreground peeks/pops with short masks around RAM access
  only. Core1 never accesses RDS storage. No IRQ mask covers a bus operation.
- Eight channels at 11.4 groups/s require about **91 handoffs/s**. Each submission
  uses two reads and nine writes, plus acknowledgement polls. Using the existing
  healthy-path delays (395 us/read, 435 us/write), base bus work is about 429 ms/s;
  polling adds load. This is a timing calculation, not a board measurement.
- A fake-time model with 87,620 us encoder slots, existing healthy bus durations
  and 1 ms foreground-loop spacing accepted **22 groups on each of eight TXs in
  2 simulated seconds**, using **1,121,680 us** bus time / **2672 transactions**.
  Host contention, IRQ load and hardware latency still need measurement on the board.
  SWD counters `host.rds_tx.bus_us`, `transactions`, and per-channel `submitted`,
  `accepted`, `polls`, `max_ack_us` support that measurement. Acknowledgement
  elapsed time includes any foreground observation delay; it is not encoder latency alone.

## CLEAR, stop/restart and failure semantics

RDS_CLEAR still immediately empties only the target RAM FIFO and returns an empty
OK response without QN work. It increments that FIFO's epoch, even if already empty.

- CLEAR before the handoff reservation cancels further preparation of that old
  group. Old data bytes already written are harmless until a handoff; all eight
  registers are rewritten before any new group's toggle.
- CLEAR after reservation cannot promise retraction of the in-progress QN handoff.
  The old group may finish. Its acknowledgement fence stays alive, but an epoch
  mismatch prevents popping **any new post-CLEAR group**. New data waits behind
  this fence. Nothing is replayed after CLEAR.
- OFF, RDS-disable, invalidated configuration or reconfiguration cancels preparation
  before handoff. An already-started handoff retains its baseline and cannot be
  replayed or overwritten. Polling pauses while ineligible; becoming eligible
  gives that existing fence a fresh bounded observation window. Stopped time does
  not create an acknowledgement timeout or an underrun. A subsequent ACK timeout
  uses the explicit RDSEN recovery above; restart alone is not acceptance.
- As in M8.6A, SET commands do not implicitly clear RAM FIFOs. The existing Linux
  stop/start, enable/disable, content/configuration and M6A recovery paths establish
  explicit CLEAR boundaries. The 2-second lookahead does not bypass those boundaries.
- A baseline/data/control read/write failure before handoff preserves the FIFO and
  faults the channel; explicit CLEAR discards old data and permits a fresh attempt.
- A failed RDSTXRDY write is **ambiguous**, even if it reported NACK/STOP failure.
  A STATUS2 read failure also retains the pending fence. ACK timeout alone uses
  the discard/reset/resync path above, not this hardware-fault path.
  No automatic group/control-write retry or subsequent submission occurs.
- Explicit CLEAR after an ambiguous fault allows a bounded **observation-only**
  reconciliation attempt against the retained baseline. A positive bit-7 transition
  clears the current RDS fault without popping new data. No transition means fault
  again on a bus/control failure, with no replay. Timeout uses RDSEN recovery;
  no ACK is fabricated. If hardware-fault reconciliation remains impossible, reset
  the RP/QNs and run normal Linux recovery. No guessed delay/resetting of a flag
  is used as acceptance. RP_SOFT_RESET remains unimplemented.
- Arbitrary raw QN writes during an unresolved handoff make attribution unreliable;
  that channel remains fenced until hardware reset. The standalone raw probe is
  retained for its historical M8.6A firmware; do not run it concurrently with this consumer.

Bus/control faults (not ACK timeout alone) set per-channel `rds_fault` and latch existing `QN_ERROR` codes.
GET_STATUS reports channel `tx_state=ERROR` while that fault is current; the
internal actual RF enable and audio source stay intact. A transport-only RDS
fault does **not** set the RP-wide critical fault, stop RF/audio or blink GPIO27.
Existing critical QN-control/audio/boot faults retain their LED/global semantics.
After explicit successful reconciliation the active channel error clears, but
`last_error_code` stays as history. RESET_ERROR_COUNTERS remains unimplemented.

Underruns count **one per genuine starvation episode**, armed only after a group
from the current FIFO/session has been accepted. Startup empty, CLEAR, OFF,
unconfigured and RDS-disabled states are unarmed. A stale post-CLEAR acknowledgement
cannot arm starvation. Refill clears the starvation episode and resumes without
RF restart. Counters saturate at u32 maximum; they are not main-loop counts.
GET_STATUS remains **130 bytes**, with existing underrun/free/error fields.
GET_RDS_FIFO_FREE remains **16 bytes**. No wire change or invented code exists.

## Linux buffering correction

M8.6B's real consumer exposed a production integration mismatch: the old **0.5 s**
lookahead could drain while the shared serialized executor waited **850 ms** for
GET_STATUS. The authorized correction changes only `LOOKAHEAD_SECONDS` to **2.0 s**
(about **23 groups**, below both the 31-group PUSH limit and 64-group FIFO).

This is **Linux buffering margin**, not a QN timing requirement. GET_STATUS's wait,
100 ms refill cadence, GET_RDS_FIFO_FREE, executor, rates/weighting, Text A/B and
CLEAR/recovery policy are unchanged. No extra queue, worker or protocol is added.
Dynamic PS remains selected from current time; queued older segments may delay
its on-air transition by the larger horizon. Configuration/RT changes retain their
existing explicit CLEAR boundary. No buffering can guarantee uninterrupted RDS
under arbitrary multi-second control stalls or sustained hardware failures.

The deterministic Linux test drains all eight queues at the unchanged
1187.5/104 groups/s while refill is unavailable for **850 ms + 100 ms phase +
0/100/250 ms jitter**, repeatedly 30 times. The real scheduler/refill default
retains data with 2.0 s; the old 0.5 s negative control starves. Existing and new
lifecycle tests confirm that the larger queue is discarded at the established boundaries.

## UART and first physical test

Use the normal Pico VS Code **Compile Project -> Flash Project (SWD)** workflow.
UART remains GPIO28/29, **115200 8N1**. Boot identifies M8.6B and all eight QNs
remain RF OFF. Capture the complete UART log from reset. Check continuous
WS=48 kHz / BCLK=1.536 MHz before RF enable; keep the existing SPI stream running
for the regression check. GPIO26 is unused throughout.

After the operator commits/pushes from Windows and updates the Pi deployment clone:

```bash
ssh fmheadend
cd ~/fmheadend
git pull --ff-only
PY=/home/fmheadend/venvs/fmheadend/bin/python
# Other I2C/control clients, including fmheadendd, must be stopped for these tools.
"$PY" firmware/fmheadend_rp2354/tools/rds_smoke.py --address 0x62
"$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cid
```

Use the terminated RF bench. The next command **explicitly enables TX0 RF** at
**87.5 MHz / 22.5 dB attenuation**, SINE, RDS enabled, PI **0x1234**, PS **FMTEST**:

```bash
set -o pipefail
"$PY" firmware/fmheadend_rp2354/tools/rds_tx_smoke.py --address 0x62 --tx 0 --rf-on \
  |& tee /tmp/m86b-tx0.log
```

The tool uses the existing Linux 0A encoder/client, queues while OFF, explicitly
enables RF, refills for 60 s, stops refill for 3 s, verifies one starvation episode
and unchanged RF ON, waits another second to check the counter is stable, then
refills for 15 s without RF restart. Finally it turns RF OFF, reads TXREQ and
CLEARs the FIFO. There are no diagnostic settling sleeps or raw register writes.
Existing synchronous host command waits still apply. On cleanup failure it prints
**RF OFF NOT VERIFIED**; physically disable bench RF. Status checks cannot prove RF.

Record receiver PI/PS and audible 1 kHz modulation, dynamically changing free counts,
one underrun during the deliberate pause, recovery without carrier restart, final
carrier absence and GPIO27 behavior. If TX0 passes, repeat separately on TX1:

```bash
"$PY" firmware/fmheadend_rp2354/tools/rds_tx_smoke.py --address 0x62 --tx 1 --rf-on \
  |& tee /tmp/m86b-tx1.log
```

TX1 uses PI **0x5678**, PS **TEST2**, the same bench frequency and attenuation.
Do not enable all eight RF outputs before establishing TX0/TX1 channel isolation.

Successful submission/acceptance logs are limited to the first three per channel
per boot. Starvation, resume, CLEAR-in-flight, faults and explicit reconciliation
are transition logs through the existing nonblocking UART drain. For an initial
baseline `00` and SYSTEM2 `03`, representative healthy lines include:

```text
RDS TX0 submitted phase=4 reason=0 bus=0 baseline=00 status=00 SYSTEM2=03->07 elapsed_us=... submitted=1 accepted=0 polls=0
RDS TX0 accepted (STATUS2 bit7 changed) phase=0 reason=0 bus=0 baseline=00 status=80 SYSTEM2=03->07 elapsed_us=... submitted=1 accepted=1 polls=...
```

These are conditional examples, **not guaranteed full-byte values or measured
timings for this firmware**. The next acknowledgement can be `80 -> 00` with
SYSTEM2 `07 -> 03`. Events may coalesce under UART backpressure; SWD counters remain.
On failure capture baseline/last STATUS2, SYSTEM2 before/after, phase, reason, bus
code, elapsed time, submission/acceptance/poll counts, command history and receiver
behavior. Reasons: 1=bus, 2=timeout, 3=reset/recalibration bits, 4=raw-write ambiguity.
Phases: 0=IDLE/baseline, 1=DATA, 2=SYSTEM2 read, 3=HANDOFF, 4=WAIT, 5=FAULT,
6=RESET, 7=RESYNC, 8=BACKOFF. ACK timeout is no longer a FAULT reason.
Do not paper over a missing toggle with sleeps or an arbitrary FIFO pop.

The separate first-call **"Unknown RP status: 128"** issue is unchanged. Retain
that error and UART trace if it occurs; these tools do not suppress/retry it.

### Final full-stack validation

After the isolated bench test, stop the tool and use the existing
[M5](milestone5.md)/[M6A](milestone6a.md) daemon
configuration/start/stop workflow, with the updated Python deployment. Use one
selected channel first; verify PS/RT and normal GET_STATUS polling alongside real
refill, with stable RDS counters while continuously fed. Exercise stop/start,
RDS disable/enable, stopped configuration/PI changes and normal recovery, checking
that pre-CLEAR queued PI/PS does not reappear (an already-handed QN group may finish).
Then validate independent channels and eight-channel bus load with SPI audio active.
No daemon scheduler, systemd or Web UI changes are part of M8.6B.

## Automated validation and remaining physical risks

Commands (existing isolated tooling; no package installation):

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build --parallel 4
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path (Get-Location) 'tmp/m83-zig-cache'
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build/native-m83 --parallel 4
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/ctest.exe" --test-dir firmware/fmheadend_rp2354/build/native-m83 --output-on-failure
& "$env:USERPROFILE/radioconda/python.exe" tmp/run_m83_pytests.py daemon/tests/test_rds.py daemon/tests/test_rp2354.py daemon/tests/test_rp2354_audio.py daemon/tests/test_recovery.py -q -ra --basetemp=tmp/m86b-pytest
```

Firmware native coverage includes all preserved M8.1-M8.6A suites, ordered exact
bytes, both acknowledgement directions, unrelated bits, no early/double pop,
all data/control/read failures, ambiguous handoff, timeout, CLEAR during each
pre-handoff operation/during toggle/during ACK read, late completion, stop/restart,
RDS enable/disable, reconfiguration, underruns, status layouts and eight-channel
fairness/fault isolation. Adapter tests assert QN/audio/UART work stays outside
IRQs/masks and SINE/RF remain active during RDS service. The bench-tool test uses
the real encoder with fake time/client, including the 850 ms status wait, starvation,
resume and exceptional RF cleanup.

Validation on Windows: production Pico SDK 2.3.1 / GCC 15.2.1 build succeeded
(`pico2`, `rp2350-arm-s`, Debug, host 0x62); **19/19 CTest suites passed**.
Focused Linux RDS/RP codec/client/audio/recovery suites: **266 passed, 1 skipped**
(real Linux i2c-dev open-failure test unavailable on Windows), with two existing
dependency deprecation warnings. `git diff --check` passed.

Remaining physical
risks are first-group/lifecycle acknowledgement behavior without diagnostic waits,
shared-bus/core0 load under eight-channel RDS plus SPI/I2S/host traffic, actual QN
behavior when disabled/reconfigured with a pending group, and final M5 full-stack
RF decoding. No final M8.6B board success or commit is claimed.

### Changed files

- Linux: `daemon/rds/scheduler.py` (one policy constant and explanation),
  `daemon/tests/test_rds.py` (serialized-gap and lifecycle tests).
- Firmware: new `rds_tx.c/.h`; `rds_fifo.c/.h` (peek/CLEAR epoch),
  `qn_control.c/.h` (session/status fields), `host_i2c.c` (foreground integration),
  `host_protocol.h` (state/version), `CMakeLists.txt`, `fmheadend_rp2354.c`, `README.md`.
- Firmware tests: new `test_rds_tx.c`, `test_rds_tx_smoke.py`; updated
  `tests/CMakeLists.txt`, `test_rds_fifo.c`, `test_host_i2c.c`, `test_rds_smoke.py`.
- Firmware tools: new `tools/rds_tx_smoke.py`; version compatibility only in
  `tools/rds_smoke.py`, `tools/qn_smoke.py`.
- Docs: this milestone, current implementation note in `rp2354-protocol.md`,
  lookahead policy in `milestone5.md` and `fmheadend-software-architecture.md`.

All firmware-relative paths above are under `firmware/fmheadend_rp2354/`.
The earlier standalone raw probe, diagnostic firmware, QN bus/boot implementation,
SPI/I2S implementation, Linux client waits/executor/refill algorithm and all
unrelated existing files are unchanged. No architecture deviation beyond the
explicitly authorized Linux lookahead policy adjustment is introduced.
