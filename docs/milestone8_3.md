# M8.3: QN8007B control from the Raspberry Pi

Production firmware `0.8.3`, protocol V1, host address **0x62**. M8.1 boot and the
proprietary software bus remain unchanged. The known-good diagnostic firmware is
unchanged. No commit or Pi-side development was performed.

## Implemented commands

- GET_INFO: V1, firmware `0.8.3`, hardware `PCB1.0`, eight physical channels.
- QN_READ_REGISTER / QN_WRITE_REGISTER: target 0..7 routes to 0x21..0x28; actual
  result, one attempt plus at most two retries per failed transaction.
- SET_TX_CONFIG: real validated 15-byte configuration, with critical register
  readback; no stub ACKs or automatic OFF-to-ON transition.
- SET_TX_ENABLED: real read/modify/write/readback of TXREQ, preserving input/RDS
  and other SYSTEM1 bits. ON requires successful boot and valid configuration;
  OFF requires no configuration. DIGITAL/SINE RF ON needs no audio engine.
- GET_STATUS: reads actual SYSTEM1 and applicable power/saturation registers in
  foreground; reports current RF request state, relative attenuation and faults.

RDS FIFO commands, RESET_ERROR_COUNTERS and RP_SOFT_RESET still return
INVALID_COMMAND. No successful reset is claimed. The Linux daemon can remain
DEGRADED because M6A recovery requires RDS_CLEAR; do not fake that recovery.

## Approved timing change

The operator approved bounded per-command synchronous waits. The former 2 ms
universal deadline could not support the proven bus: single-register reads use
395 us and writes 435 us of explicit delays; five writes already exceed 2 ms.
The prior diagnostic also waits 20 ms after configuration. These software values
are not new measurements of the board.

V1 framing, command IDs, payload sizes, STOP boundaries, one outstanding request
and two-read cursor behavior are retained. The Linux client now waits as follows
before starting the two-byte response-header read:

| Command | Response deadline / Linux wait | Conservative QN work bound |
|---|---:|---:|
| GET_INFO and unsupported/other commands | 2 ms | No QN operation |
| QN_READ_REGISTER / QN_WRITE_REGISTER | 100 ms | At most two register accesses: 66 ms |
| SET_TX_ENABLED | 150 ms | Three accesses: 99 ms |
| GET_STATUS | 850 ms | At most 24 accesses: 792 ms |
| SET_TX_CONFIG | 1000 ms | 28 accesses plus settling: 944 ms |

Each access permits three attempts. The existing bus has a 10 ms transaction
limit and bounded STOP cleanup; using 11 ms per attempt gives a conservative
33 ms allowance per access. Configuration retains the board-proven 20 ms settling
interval. Failure cleanup directly lowers CEN and adds no bus operations. The
remaining margin covers foreground dispatch and short interrupt/serialization
work. Tests exercise timeouts before every successful access; physical response
latency, especially under host traffic, still needs a logic-analyzer check.

The IRQ drains/captures requests and handles response bytes, never QN traffic or
UART output. GET_INFO and validation/unsupported-command errors are immediate.
Other accepted commands occupy one foreground work slot and publish OK only on
completion. Work/result handoffs mask interrupts briefly; bitbanging does not.
UART emits at most 16 ready characters per poll and never waits for FIFO space.
The existing bounded log queue can reject new requests with BUSY when full.

BUSY means that request did not execute and will never execute later. An early
read cancels unstarted work with BUSY. Once work starts, an early read returns
INTERNAL_ERROR (outcome unknown), never BUSY or early OK; the eventual result
cannot replace that error. These early reads violate the deadline contract and
are not completion polling. An overlapping new request is rejected with BUSY;
its response is not overwritten by completion of the previous operation.

Update firmware and Linux client together. The unchanged M8.2 `host_smoke.py`
refuses firmware versions other than `0.8.2-stub`; use `qn_smoke.py` for M8.3.

## Hardware review

The actual `firmware/fmheadend_i2ctest/fmheadend.pdf`, revision 1.0 dated
2026-09-19, was rendered and visually inspected on sheets 2 and 4.

- Host SDA/SCL remain GPIO14/15, I2C1, configured address `0x62`.
- QN SDA/SCL remain GPIO16/17, separate from the host bus.
- CEN0..7 remain GPIO18..25 with external pull-downs. MOD is LOW and SEB HIGH.
- GPIO27 is the active-HIGH diagnostic LED; UART0 remains GPIO28/29, 115200 baud.
- Each QN has a 26 MHz, 10 pF crystal.
- **Schematic discrepancy with architecture section 2:** only TX0..TX3 have
  connected analog inputs. TX4..TX7 ALI/ARI are marked no-connect. TX0 has J2;
  the four analog pairs also appear at J8 on the MCU sheet. An analog setting on
  TX4..TX7 cannot provide externally connected analog audio on this schematic.
- The connected analog paths have 220 ohm series resistors and 1 uF coupling
  capacitors. These resistors are not the QN's programmable RIN setting. Gain
  specified at QN input pins must not be presented as a calibrated connector-to-
  modulation gain.

## Datasheet findings for implementation

Source: Quintic **QN8007B/8007LB Rev 2.09 (11/09)**,
[manufacturer datasheet hosted by DZSC](https://file2.dzsc.com/icpdf/18/07/11/671969_134915924.pdf),
section 7. Implemented mappings:

| Register | Relevant mapping | Page |
|---|---|---:|
| SYSTEM1 `00h` | TXREQ bit 6; TXI2S bit 2; RDSEN bit 1; explicit channel selection bit 0 | 26-27 |
| SYSTEM2 `01h` | RECAL bit 6; mono bit 4; 75 us bit 3; timeout bits 1:0 | 27 |
| REG_VGA `04h` | RIN bits 7:6 select 10/20/40/80 kohm; crystal loading bits 5:0 | 29 |
| I2S `07h` | `0x71`: 48 kHz, 16-bit, slave, standard I2S; diagnostic `0x41` is 32 kHz | 31 |
| CH `08h`, CH_STEP `0Bh` | Channel index `(frequency_khz - 76000) / 50`; high bits in `0Bh[1:0]` | 32-33 |
| TXAGC GAIN `0Dh` | Clipping bit 7, MANUAL bit 6, +1 dB trim bit 4, analog gain code bits 3:0 | 34 |
| STATUS1 `1Ah` | Analog saturation bit 3 | 40 |
| PAG_CAL `5Ah` | Disable automatic gain calibration with bit 6; preserve IPOW bits 5:4; relative attenuation code in bits 3:0 | 42-43 |

RIN is programmable, not an external board resistor code. The working diagnostic
uses RIN=01 (20 kohm); that gain-table column has codes 0..11 corresponding to
-1.5..15 dB in 1.5 dB steps. Codes 12..15 are reserved. No absolute RF calibration
is established by this review.

## Configuration sequence and exact settings

Validation precedes all hardware effects: 76000..108000 kHz on a 50 kHz raster;
attenuation 0..225 tenths in steps of 15; booleans, 50/75 us, DIGITAL=0, ANALOG=1,
SINE=2, AUTO/MANUAL, digital trim 0/1. MANUAL gain is -1.5..15 dB in exact 1.5 dB
steps for the proven RIN=01 setting. Unsupported values return INVALID_ARGUMENT.
AUTO ignores carried manual gain/trim fields as specified by V1. No rounding,
clamping, user-default configuration, PAC_TARGET approximation or absolute output
calibration is introduced.

The sequence reads SYSTEM1, saves its ON state, clears TXREQ and verifies OFF.
It reads CH_STEP, PAG_CAL and SYSTEM2 for preserved fields, holds RECAL, then
writes/verifies SYSTEM1, ANACTL1, REG_VGA, I2S, CH, CH_STEP, TXAGC GAIN and PAG_CAL.
It releases RECAL, waits 20 ms, then verifies the final SYSTEM1 value, restoring
ON only if this was an already-active transmitter. Configuration becomes valid
only after every operation succeeds. Invalid requests leave the previous valid
configuration and RF state unchanged.

ANACTL1 `0x2b` selects the board's 26 MHz crystal, normal baseband operation and
unmuted/noninverted input. REG_VGA `0x60` preserves the working RIN/crystal-load
choice. SYSTEM2 preserves the RDS handshake, selects stereo/emphasis and disables
idle timeout; it never uses SWRST. PAG_CAL preserves IPOW and its reserved bit,
sets PAG_DIS and maps attenuation to PAGAIN. No PAC_TARGET or invented RF
calibration is used. AUTO gain readback ignores AGC-owned gain bits; clipping and
manual selection are still verified. The register definitions above and the
unchanged reference establish these choices; oscillator/PLL/RF behavior still
requires the bench test. The 20 ms delay is a proven reference interval, not a
newly claimed datasheet maximum or a measured PLL-lock indication.

ANALOG selects the chip's analog input; on PCB1.0 external audio exists only on
TX0..TX3. Selection is accepted on all eight chips, but TX4..TX7 have no connected
analog source. Use TX0/J2 for the first analog test. Gain is the documented QN
input-buffer gain, not a calibrated gain including external resistor/source loss.
DIGITAL and SINE both configure 48 kHz / 16-bit stereo I2S. SINE is exposed through
`TxConfig` and the low-level Linux codec/client only; no WebUI/source orchestration
or generator was added.

`rds_enabled` controls the documented QN RDSEN bit. There is no RDS FIFO, data
loading, handshake polling, scheduler or assurance of transmitted RDS content.
Use `rds_enabled=false` for this bench test.

## Failure handling, raw diagnostics and status

Any exhausted QN transaction or critical readback mismatch returns QN_ERROR and
lowers the affected CEN. This guarantees RF disabled even if the last write had
an ambiguous outcome; configuration is invalidated, the channel becomes ERROR
and unavailable, and GPIO27 blinks. Other channels retain their state. Reset the
RP after repairing the fault to rerun M8.1 assignment; there is no automatic
runtime CEN recovery. Failed boot channels likewise report ERROR, with RF held
off. OFF against an unavailable QN returns QN_ERROR, not fake success.

Raw access accepts all u8 registers, including SYSTEM1. Successful SYSTEM1
writes/readback update enabled/input state immediately; raw TXREQ may turn RF on
without a validated configuration, intentionally. TXREQ-only changes preserve a
previous valid configuration; other raw configuration overrides invalidate it.
Raw PAG_CAL readback updates relative attenuation when manual steps are selected,
otherwise it is unavailable. Raw DEV_ADD changes and SYSTEM2 SWRST execute, then
isolate that channel to prevent address collisions; their successful ACK does
not claim the channel remains available. Reset RP to restore assigned routing.
A failed raw access also returns QN_ERROR and isolates the channel.

GET_STATUS samples SYSTEM1, applicable PAG_CAL and analog STATUS1.INSAT. Digital
and SINE saturation is unavailable (255). RF state is confirmed control-register
state, not a measurement of emitted power or PLL lock. Current faults determine
global ERROR/channel ERROR and blinking. Latched error codes and internal
saturating failed-transaction counters remain separate; retry success does not
create an active fault. The latter counters are inspectable in `host.qn` through
SWD; V1 has no QN-transaction-counter field. SPI/audio/RDS counters remain zero
because those monitors are not implemented, and RDS FIFO free is zero.

GPIO27: healthy idle OFF, any active TX without active fault steady ON, current
fault blinking. LED updates occur in foreground; a bounded QN command may delay
a visual update. Boot still begins with every CEN and the LED LOW, assigns all
QNs sequentially and confirms RF OFF before enabling host I2C.

## Build and automated validation

The normal Pico VS Code **Compile Project -> Flash Project (SWD)** workflow uses
`firmware/fmheadend_rp2354/build/fmheadend_rp2354.elf`. No extension task/target
change is required. Equivalent Windows build from the repository root:

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" `
  --build firmware/fmheadend_rp2354/build --parallel 4
```

For a fresh build cache, use the M8.2 configure command with
`-DFMHEADEND_HOST_I2C_ADDRESS=0x62`. The production build remains Pico SDK 2.3.1,
ARM GCC 15.2.1, Debug, pico2 / rp2350-arm-s. ELF/BIN/UF2 are generated.

Native tests on a host with a native C compiler:

```sh
cmake -S firmware/fmheadend_rp2354/tests -B firmware/fmheadend_rp2354/build/tests
cmake --build firmware/fmheadend_rp2354/build/tests
ctest --test-dir firmware/fmheadend_rp2354/build/tests --output-on-failure
python -m pytest daemon/tests/test_rp2354.py daemon/tests/test_rp2354_audio.py -q -ra
```

Recorded on 2026-09-21:

- Production ARM Debug build: passed; ELF/BIN/UF2 generated, address 0x62.
- Native CTest: **5/5 passed** on Windows, portable Zig 0.15.2 / Clang 20.1.2,
  `-Wall -Wextra -Werror`, in `build/native-m83`.
- Focused Python client/codec/audio-control regression: **200 passed, 1 skipped**
  (Linux-only device-open test), two dependency deprecation warnings.
- `qn_smoke.py --help`: passed without opening any hardware.
- `git diff --check`: passed. Diagnostic firmware, QN bus and boot sources unchanged.

Temporary test tools/dependencies were isolated under ignored `tmp/`; KiCad and
system Python environments were not modified. Existing M8.1
bus tests and 74 safe-boot scenarios are retained. M8.2 framing/adapter regression
cases remain, with stub-specific assertions replaced by real execution checks.
New coverage includes all routes, tuning/output/gain mappings, both inputs/SINE,
retry limits, every configuration-operation failure, reconfiguration, ON/OFF,
raw state, LED, overload, early reads, overlap and bounded response timing.

## Exact first physical tests

Windows remains authoritative. The operator flashes and tests, then commits.
To test the uncommitted client/script without changing the Pi Git clone, stage a
separate test snapshot from Windows (Git Bash or PowerShell, repository root):

```powershell
$benchTag = Get-Date -Format 'yyyyMMdd-HHmmss'
$benchArchive = "tmp/m83-bench-$benchTag.tar"
tar -cf $benchArchive --exclude=__pycache__ --exclude=*.egg-info daemon firmware/fmheadend_rp2354/tools
scp $benchArchive "fmheadend:/tmp/m83-bench-$benchTag.tar"
```

On the Pi use the printed timestamp in place of STAMP. This extracts the exact
Windows sources into an isolated test directory; it does not edit the deployment
clone or create a Pi commit:

```sh
bench=$(mktemp -d /tmp/fmheadend-m83.XXXXXX)
tar -xf /tmp/m83-bench-STAMP.tar -C "$bench"
cd "$bench"
```

Use the existing Python environment containing the daemon's dependencies:

```sh
PY=python3  # Or the absolute path to the existing daemon venv's bin/python.
"$PY" -c 'import pydantic; print(pydantic.__version__)'
```

If that environment is unavailable, the operator can prepare an isolated bench
venv (requires the Pi's existing Python venv support):

```sh
python3 -m venv "$bench/.venv"
"$bench/.venv/bin/python" -m pip install ./daemon
PY="$bench/.venv/bin/python"
```

This optional setup installs only into the temporary venv; it creates no service. Stop any running control
client/daemon first so only this test owns `/dev/i2c-1`. If necessary, run the
commands with `sudo "$PY"` for device access. Do not use SMBus register tools.

1. Keep the conducted RF output terminated on the existing bench. Open UART
   115200 8N1, no flow control, DTR asserted. Compile and flash the production
   project through SWD. Require eight CID2 successes, `BOOT: 8/8 ready; RF OFF;
   IDLE`, LED OFF and `HOST I2C1 ... address=0x62; V1 M8.3 QN CONTROL; RF OFF`.
2. The first test only reads identity through Pi -> RP -> QN -> RP -> Pi:

   ```sh
   "$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cid
   ```

   Require eight lines, TX0/0x21 through TX7/0x28, each CID2 product nibble 1
   (this board previously returned 0x11). No SET command or RF enable is sent.
3. Test one frequency and conservative selected attenuation, with no SPI/I2S
   prerequisite:

   ```sh
   "$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cycle \
       --tx 0 --frequency-khz 98100 --attenuation-db 22.5 --mode DIGITAL --hold-seconds 10
   ```

   The script refuses unexpected firmware, active faults or other active TXs,
   verifies rejection of 98101 kHz, configures 98100 kHz, verifies OFF/readback,
   holds OFF, turns ON, holds ON, then turns OFF in a finally block. Observe the
   98.100 MHz carrier on the analyzer only during ON, with configured relative
   attenuation 22.5 dB, target status ON and LED steady ON. Expect no generated
   digital audio. OFF must remove the carrier and leave LED OFF. UART should show
   real OK responses, never `STUB OK`.
4. Optional analog repeat: use `--mode ANALOG` and feed TX0/J2 externally. Optional
   `--mode SINE` only tests enum/digital-input selection: it does not generate a
   tone in M8.3. No audio path is required for the RF carrier test.
5. Expected UART examples:

   ```text
   HOST RX: QN_READ_REGISTER TX0 reg=0x06 value=0x11
   HOST TX: OK payload=1 bytes
   HOST RX: SET_TX_CONFIG TX0 frequency_khz=98100 ...
   HOST TX: OK payload=0 bytes
   HOST RX: SET_TX_ENABLED TX0 ON
   HOST TX: OK payload=0 bytes
   HOST RX: SET_TX_ENABLED TX0 OFF
   HOST TX: OK payload=0 bytes
   ```

6. Capture GPIO14/15 and GPIO16/17. Verify a request STOP, the documented wait,
   two-byte header read, STOP and payload read continuing at byte zero of payload.
   QN reads must send address+READ, then the master-transmitted register byte,
   then data with final NACK. Check response completion before each deadline.
7. With a suitable open-drain fault fixture, repeat a CID read while QN SDA or
   SCL is held LOW. Expect bounded QN_ERROR, CEN LOW for the addressed channel,
   ERROR status and blinking LED; no false OK. Remove the fixture and reset RP
   before normal testing. Never short a push-pull CEN output. An I2C failure or
   failed final OFF cannot prove RF absent: reset RP and verify the bench output.
8. Repeat cold boot and RP-only reset: all TXs must return OFF and lose runtime
   configuration. Record UART, analyzer/scope traces, board revision and ELF hash.

After operator acceptance/commit, use the established `git push pi` and Pi
`git pull --ff-only` workflow. No Git remotes are changed by M8.3.

## Remaining audio/RDS work (documented only)

The future audio engine starts at boot independently of TX enable and mode:
shared WS=48 kHz, BCLK=1.536 MHz, and eight individual DATA lines. DIGITAL uses
valid demultiplexed SPI PCM or zeros when missing. SINE will generate 1 kHz at
-12 dBFS. ANALOG may receive zero DATA because QN uses its analog input. RF ON is
not gated on audio availability. No I2S, PIO/DMA audio, sine generator, SPI audio,
RDS FIFO/scheduler or QN interrupt processing was implemented. Future RDS status
uses polling, not QN_INT. There is no SET_TEST_MODE or transmitter rotation.

No M8.3 board flash, RF measurement, audio test or physical timing test was
performed by the implementation agent. Successful compilation/native mocks do
not replace these operator tests.

## Changed files

Production: `qn_control.c/.h` (new), `host_protocol.c/.h`, `host_i2c.c/.h`,
`fmheadend_rp2354.c`, `CMakeLists.txt`, `README.md` under
`firmware/fmheadend_rp2354/`. New bench tool: `tools/qn_smoke.py`.

Tests: new `tests/fake_qn.c/.h` and `tests/test_qn_control.c`; updated native
CMake, UART fake declarations and existing host/boot harnesses.

Linux: `daemon/rp2354/client.py`, `protocol.py`, `linux_client.py`, and
`daemon/tests/test_rp2354.py`. Documentation: this file and
`docs/rp2354-protocol.md`. No WebUI, audio backend, M6A or diagnostic-project files
were changed. The sole protocol timing deviation was explicitly approved;
SINE=2 was explicitly requested within unreleased V1.
