# Firmware-side TX0 RDS probe: 60 seconds at QN handshake pace

This is an explicitly enabled bench variant of M8.6A, **0.8.6a-probe**. It is not
the production M8.6B FIFO consumer. All eight 64-group FIFOs and their status
fields remain unchanged and unused by this probe. GPIO26/QN_INT is entirely
unused; RDS interrupt output is never enabled.

## Physical evidence and purpose

The operator's raw-register probe repeatedly established that writing RDSD0..7
alone did not toggle STATUS2 bit 7, and that every RDSTXRDY toggle was followed by
a STATUS2 bit-7 toggle. This polling handshake is now a physically established
project input. The old temporary bench attempt was not a proven reference.

The slow Linux raw-register experiment did not produce receiver RDS indication.
Its approximately 2.4-second group interval, caused by existing 100 ms host waits,
was not a sustained RDS test. This firmware probe removes those host round trips
from group submission. High-byte-first content/receiver decoding still needs this
physical run; no new on-air success is claimed.

## Activation and scope

`FMHEADEND_TX0_RDS_PROBE` is a CMake option, **OFF by default**. With it OFF, the
probe source is not compiled and firmware remains 0.8.6a. With it ON:

- Boot still performs normal eight-QN assignment, RF OFF, continuous I2S and SPI
  initialization. The probe waits, without QN traffic, for a normally configured
  TX0 with RDSEN and RF enabled. Nothing enables RF automatically at boot.
- The Pi launcher explicitly configures/enables TX0 with normal V1 commands:
  **87.5 MHz, 22.5 dB attenuation, DIGITAL, 48 kHz/16-bit I2S, stereo, 50 us,
  RDS enabled, AUTO gain, soft clipping OFF**. It sends no group data or raw writes.
- The local probe verifies SYSTEM1=47, CH=E6, CH_STEP low bits=0, I2S=71, and
  RDS_INT_EN=0; SYS2 must not have SWRST/RECAL set. Other TXs must be OFF and
  production state must have no active fault. A mismatch stops the bench run.
- One run per hardware reset. After **60 seconds**, firmware performs the existing
  verified TX0 OFF operation. It does not automatically restart. The deadline is
  checked by the foreground loop, so bounded bus/host work can delay the OFF
  operation slightly. Keep all other control clients stopped during the test.
- Temporary diagnostic settling: the Pi launcher waits **300 ms after successful
  SET_TX_CONFIG** before issuing SET_TX_ENABLED ON. This is additional to the
  existing client's command-response wait. On observing RF ON, the firmware waits
  **300 ms before the first group**, using a nonblocking deadline. The 60-second
  group-transfer window starts afterward (RF is ON for roughly 60.3 seconds).
  Subsequent groups have no settling delay. These two diagnostic intervals can
  later be removed independently; neither changes normal production behavior.
- A host TX0 SET_CONFIG/SET_ENABLED/raw-write request cancels the probe before
  executing that command; that command owns the subsequent RF state. This adds
  no extra QN transaction to the host command deadline. GET_INFO/GET_STATUS remain
  usable. Operator interruption via normal TX0 OFF cancels the run.

Fixed repeating bench content is a deliberate, narrowly scoped exception to the
production Linux-only content rule. There is no production scheduler, FIFO
consumer, new command ID, SPI format change, audio change or automatic TX rotation.

## Exact content and handoff

The four groups match the existing Linux encoder:
`group_0a(0xffff, 0, False, b" R D S  ", segment, stereo=True, af=())`.
PS is exactly eight bytes: `20 52 20 44 20 53 20 20`.

| Segment | A B C D | RDSD0..RDSD7, high byte first |
| --- | --- | --- |
| 0 | FFFF 0008 E0CD 2052 | FF FF 00 08 E0 CD 20 52 |
| 1 | FFFF 0009 E0CD 2044 | FF FF 00 09 E0 CD 20 44 |
| 2 | FFFF 000A E0CD 2053 | FF FF 00 0A E0 CD 20 53 |
| 3 | FFFF 000F E0CD 2020 | FF FF 00 0F E0 CD 20 20 |

For each group:

1. Snapshot STATUS2 `0x1B` before writing data.
2. Write the eight bytes to `0x10..0x17` locally through the proven QN software bus.
3. Toggle only SYSTEM2 `0x01` bit 2 (`XOR 04`), retaining the initial SYSTEM2's
   other bits. External TX0 mutations cancel the run, so they cannot silently
   invalidate this shadow value.
4. Poll STATUS2 at a minimum **2 ms** interval. Accept when
   `(observed ^ baseline) & 0x80` is nonzero, in either direction.
5. Advance the segment modulo four and submit the next group in the **same service
   call**. There is no host wait, artificial group delay, sleep-until-ready loop
   or scheduler pacing timer.

The foreground core0 loop owns all QN traffic; host command execution takes
priority. Probe calls run outside the I2C IRQ and outside interrupt masks. A
healthy group submission takes roughly 4.3 ms of existing bitbang delays (one
STATUS2 read, eight data writes, one SYSTEM2 write), plus execution/interrupt
overhead. Polling reads take the existing roughly 395 us. The main loop's 1 ms
sleep makes actual polling intervals slightly larger than 2 ms. Core1 SPI/I2S
resource ownership and clocks do not change. Local RDS writes preserve the
production TX configuration record, unlike the raw diagnostic host commands.

A failed bus transaction stops immediately: no probe retry, replay or guessed
acceptance. If bit 7 remains unchanged for **500 ms**, report the baseline/latest
STATUS2 and SYSTEM2, stop and attempt normal RF OFF. A fault latches existing
QN_ERROR and marks TX0 faulted (LED blinks); explicit hardware reset is required
for another probe. Existing QN OFF failure isolation remains in force. No RDS
underrun counters are invented, since no FIFO consumer has been added.

The nominal RDS rate is approximately 11.4 groups/s, roughly **680 groups in 60 s**.
UART accepted counts, elapsed time, poll counts and maximum observed handshake
time measure the real local rate. Counts are buffer-acceptance observations, not
proof of RF decoding. One group may remain pending when the 60-second run ends;
submitted/accepted counts can differ by one.

## Build and first physical run

Both normal and probe variants were built successfully. The current local
`firmware/fmheadend_rp2354/build` cache is intentionally left with the probe
option **ON**, so the usual VS Code **Compile Project -> Flash Project (SWD)**
workflow builds/flashes **0.8.6a-probe**. To reproduce from the Windows repo root:

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" -S firmware/fmheadend_rp2354 -B firmware/fmheadend_rp2354/build -DFMHEADEND_TX0_RDS_PROBE=ON
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build --parallel 4
```

Flash `build/fmheadend_rp2354.elf` via the existing SWD workflow. Reset the RP;
confirm all QNs ready, audio running, RF OFF, and the `LOCAL RDS ... ARMED` UART
line. UART remains 115200 8N1 on GPIO28/29. Use the terminated bench and tune the
receiver to **87.5 MHz**. Stop all other I2C/control clients. Use the normal
operator-owned Git deployment; no commit/push or Pi changes were made here.

On the Pi, after deploying the launcher:

```bash
cd ~/fmheadend
git pull --ff-only
PY=/home/fmheadend/venvs/fmheadend/bin/python
set -o pipefail
"$PY" firmware/fmheadend_rp2354/tools/qn_rds_local_probe.py --address 0x62 \
  |& tee /tmp/qn-rds-local-pi.log
```

**This command explicitly enables RF on TX0.** The launcher polls status every
two seconds, watches for timed OFF, and attempts/verifies OFF on exit. It refuses
plain 0.8.6a to avoid running an unbounded carrier without the firmware timer.
Reset the RP before another run. A launcher disconnect does not stop the
firmware's 60-second timer.

Capture receiver PI **FFFF**, PS **` R D S  `** (receiver may trim spaces), RDS
indication and the entire UART trace. Measure BCLK/WS if checking concurrent audio;
the existing 48 kHz clocks must remain continuous. DIGITAL remains silent when no
SPI source is supplied; no sine source is required.

## UART evidence

At boot:

```text
LOCAL RDS: 0.8.6a-probe ARMED; boot RF OFF; host must configure/enable TX0; one run per reset
```

At start, then the first four transfers (register values/times are observations):

```text
LOCAL RDS: TX0 START 60s; 87500 kHz; PI=FFFF PS=' R D S  '; high-byte-first; POLLING ONLY
LOCAL RDS: TX0 submit=1 segment=0 STATUS2=0x00 SYSTEM2=0x03->0x07
LOCAL RDS: TX0 STATUS2 0x00->0x80 at <measured> us
LOCAL RDS: TX0 accepted=1 segment=0 bit7 toggled; elapsed_us=<measured>
```

Later lines summarize every ten seconds and finish with:

```text
LOCAL RDS: 60s COMPLETE; submitted=<n> accepted=<n or n-1> polls=<n> max_ack_us=<measured> dropped_logs=0 RF_OFF=OK
```

Logging uses an eight-entry bounded queue, drained by the existing nonblocking
foreground UART path. There is no indefinite successful-per-group logging. If
failure occurs, retain `READ/WRITE FAIL reg=... bus_status=...`, or `TIMEOUT
baseline=... last=... SYSTEM2=... elapsed_us=...`, and the final OFF result. No
delays, acceptance assumptions or interrupt fallback are added to hide failure.

After this bench experiment, restore the normal variant and reflash:

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" -S firmware/fmheadend_rp2354 -B firmware/fmheadend_rp2354/build -DFMHEADEND_TX0_RDS_PROBE=OFF
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build --parallel 4
```

## Validation and changed files

- Normal and enabled-probe Pico SDK builds passed.
- **20/20 CTest suites passed**, retaining M8.1-M8.6A, SPI/I2S/host/QN tests.
- New simulated 60-second run checks exact group bytes/order, both toggle
  directions, no next group before acknowledgement, immediate next submission,
  normal-rate throughput, timed OFF, one-shot behavior, data/control/read errors,
  timeout, wrong configuration and disabled interrupt output.
- Host adapter tests run with both compile options. The launcher has four tests
  including comparison with the real Linux encoder, version checks and cleanup.
- No physical run or commit was performed by the agent. Handshake evidence above
  is the operator's prior result; sustained on-air RDS reception remains unproven.

Added `qn_rds_local_probe.c/.h`, `tools/qn_rds_local_probe.py`, two probe/launcher
tests and this document. Updated production/test CMake, `host_protocol.h` version
selection, `host_i2c.c` conditional foreground integration, and optional fake-QN
write tracing for tests. QN register mapping/bus, SPI, I2S, Linux encoder/scheduler,
FIFO transport and wire layouts are unchanged.
