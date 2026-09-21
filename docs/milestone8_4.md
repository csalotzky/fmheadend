# M8.4: continuous eight-channel I2S and SINE

Production firmware **0.8.4**, protocol **V1**, host I2C **0x62**. The M8.1
safe boot, M8.3 QN register mappings, RF commands and approved response waits
are preserved. No commit, Pi-side edit, flash or physical measurement was made
by the implementation agent. Hardware acceptance remains the operator's task.

## Signals and exact format

Checked against `firmware/fmheadend_i2ctest/fmheadend.pdf`, PCB1.0, sheets 2/4
and 4/4: DATA0..7 = GPIO4..11 through 22-ohm series resistors to each QN DIN;
BCLK = GPIO12 to shared QN MCK; WS = GPIO13 to shared QN WS. TP1 is WS,
TP2 BCLK and TP3 DATA0. No schematic/pin discrepancy was found.

One PIO0 state machine drives all ten pins. Each OUT emits one bit on all eight
lanes simultaneously. Two SM cycles LOW and two HIGH give four cycles per BCLK;
16 bits per channel give 32 BCLKs / 128 SM cycles per stereo frame. There are
no padding bits, 32-bit slots or boundary instructions adding clocks.

| Quantity | Value |
|---|---|
| PCM | 48,000 stereo frames/s, signed 16-bit two's complement |
| Serialization | MSB first, left then right, rising-edge receive |
| WS | 48 kHz; LOW selects left, HIGH right |
| I2S alignment | WS changes on the previous word's LSB, one BCLK before the next MSB |
| BCLK | 1.536 MHz, nominal 50% duty cycle |
| PIO SM | 6.144 MHz; system 150 MHz / (24 + 106/256) |

The fractional divider gives exact **average** rates at nominal 150 MHz;
individual edges have system-clock quantization (one cycle = 6.67 ns).
Crystal tolerance still applies. An unrepresentable system clock is an explicit
initialization failure, not silently rounded. The initial partial I2S word is
zero; the receiver synchronizes at the first WS transition.

The old diagnostic PIO has extra boundary cycles and a different WS transition
position; its assembly was not copied. The known-good diagnostic project is
unchanged. QN register 07h remains **0x71**, as already established in M8.3:
16-bit, 48 kHz, slave, I2S format 1. See the Quintic QN8007B/8007LB
[datasheet rev 2.09](https://file2.dzsc.com/icpdf/18/07/11/671969_134915924.pdf),
section 6.3.2, p.23 for alignment and section 7, p.31 for register 07h.
There is no 32 kHz setting or other QN register-mapping change.

## Continuous feed and source selection

After safe QN boot and before host I2C initialization, core 1 starts the engine
with silence. BCLK/WS run continuously even with all TXs OFF, unconfigured, or
ANALOG. No host command starts, stops or restarts the engine.

Two DMA channels chain alternately into the joined PIO TX FIFO, paced by its
DREQ. Each shared packed buffer contains 128 stereo frames / 1,024 words /
4 KiB: **8 KiB total**, not eight sets of audio buffers. Four chronological
eight-lane bytes occupy one 32-bit DMA word, shifted right by PIO. Eight such
words encode a stereo frame. The buffers are aligned read rings for containment
of a late rearm, not repeating waveform loops.

Core 1 polls completion and refills the inactive buffer while the other plays
for 2.667 ms. It disables the free channel before writing, checks that it has
not retriggered, and rearms before its peer finishes. There is no audio IRQ,
allocation, UART, QN transaction or core-0 scheduling dependency in this path.
Core 0 publishes an atomic eight-bit source mask; no cross-core lock is needed.

- OFF, unavailable or invalidated configuration: zero PCM.
- Enabled DIGITAL or ANALOG: zero PCM. Missing SPI is intentional silence.
- Enabled, successfully configured SINE: local 1 kHz stereo sine, identical L/R.

The 48-entry table is `round(32767 * 10^(-12/20) * sin(2*pi*n/48))`, peak
**8231** PCM counts. This specifies peak sine level relative to full scale,
not RF deviation or QN analog gain. Phase advances through every frame, including
silence, and persists across 128-frame boundaries (128 is not divisible by 48).
Active sine lanes share phase but selection is independent. A source change
takes effect at a refill boundary, normally within about 5.4 ms including FIFO
latency. Mode switches are hard switches, without fades; there is no periodic
phase reset/click at DMA boundaries.

SET_TX_CONFIG stores the selected mode only after success. Reconfiguration
mutes that lane while QN work runs; other lanes and shared clocks continue.
Invalid arguments do not alter the source. Failed QN operations retain M8.3
CEN isolation and mute the failed lane. Raw overrides that invalidate a complete
configuration also mute its source. RF state remains the observed QN state.

## Failure and status behavior

### Boot failure investigation after the first physical attempt

The operator reported immediate blinking, global INTERNAL_ERROR, no observable
BCLK/WS, and working access to all eight QNs. Physical audio acceptance has **not**
passed. Changes in this investigation are restricted to the audio engine,
diagnostics, its tests and this record; the working M8.3 QN controls are unchanged.

A definite bug was found in DMA rearming: setting EN through `CTRL_TRIG` also
**triggers** the channel. The inactive channel could therefore start alongside
its active peer instead of waiting for CHAIN_TO. EN changes now use `AL1_CTRL`,
the non-triggering alias. This is the leading code-based explanation for a very
early stop, but it would permit a short initial clock burst; it is not proof of
the board's exact failure stage. Capture UART and, if needed, a scope trace
triggered on reset to distinguish an early stop from clocks never starting.
See the [RP2350 datasheet](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf),
section 12.6.3.1, and Pico SDK 2.3.1 `dma_channel_set_config(..., false)`, which
likewise uses AL1_CTRL to avoid triggering. The original native mock treated
CTRL_TRIG as ordinary RAM and missed this effect; it now models the trigger.

The initialization order was compared with the known-working diagnostic:
configure PIO and DMA, prepare data, start DMA, then enable the SM. M8.4 additionally
primes the joined TX FIFO while the SM is disabled, with a 1 ms timeout. That is
valid because the FIFO/DREQ can accept DMA writes while instruction execution is
disabled. The reference sets pin directions/levels before `pio_sm_init`; M8.4
sets them immediately afterward, while disabled. The SDK helpers save/restore
PINCTRL/EXECCTRL, and neither ordering inherently prevents startup. X=14 must
remain seeded after SM initialization. No speculative reordering, clock rounding,
QN change, timeout relaxation or fault suppression was made.

Every existing route to an audio fault during boot or immediately afterward is
now distinguishable on UART:

| Failed stage / reason | Condition and evidence printed |
|---|---|
| clock / CLOCK FAULT | Exact divider unavailable, out of range or zero system clock; sys_hz, target SM Hz, div256 |
| SM claim / RESOURCE FAULT | No free PIO0 SM; SM=-1 |
| program load / RESOURCE FAULT | No instruction space, or negative SDK load result; offset and sdk_rc |
| DMA claim / RESOURCE FAULT | Either DMA allocation fails; both channel numbers |
| SM config or pin directions / PIO CONFIG FAULT | Nonzero `pio_sm_init` or pin-direction return; distinct stage and sdk_rc (previously unchecked) |
| core1 launch/ready or buffers / REFILL DEADLINE | Post-launch 100 ms timeout before RUNNING; last stage and milestone bits |
| FIFO prime / DMA FAULT | TX FIFO not full within 1 ms; FIFO state, DMA counts, CTRL/error bits and addresses |
| PIO start / REFILL DEADLINE | Core 0's startup timeout won before core 1 could publish RUNNING |
| refill / PIO STALL | TXSTALL observed after enabling; pre-cleanup FDEBUG and PC |
| refill / DMA FAULT | DMA READ/WRITE bus error; pre-cleanup channel CTRL/addresses |
| refill / REFILL DEADLINE | Wrong/both completion flags, free channel already busy, peer inactive, peer finishes during refill, or no progress for 6 ms |

The SDK core-launch handshake itself has no new timeout: if it hangs, UART ends
at `core1 launch` and host initialization is never reached. That does not match
the reported working host/QN access. DMA configuration is register writes with
no SDK error return; FIFO priming verifies actual DMA progress before PIO starts.
Fault snapshots precede disabling/aborting hardware, so cleanup does not erase
the diagnostic evidence. Stage markers are published by core 1; all UART output
is on core 0, never in the refill loop or an IRQ. The global INTERNAL_ERROR and
blinking LED remain intact.

Expected successful next boot (normally SM0, offset 24, DMA0/1; buffer addresses
depend on the build) includes exactly these stage lines, between the QN boot
summary and existing audio/host banners:

```text
AUDIO: init start
AUDIO: clock sys_hz=150000000 target_sm_hz=6144000 div256=6250
AUDIO: SM claimed PIO=0 SM=0
AUDIO: PIO program loaded offset=24 instructions=8
AUDIO: DMA channels claimed A=0 B=1
AUDIO: SM configured DATA=4..11 BCLK=12 WS=13 div=24+106/256 PC=24
AUDIO: DMA configured words=1024 ring_bytes=4096 DREQ=0 A=<address> B=<address>
AUDIO: core1 launch
AUDIO: core1 launched
AUDIO: core1 ready
AUDIO: buffers prepared frames=128 each; silence
AUDIO: DMA started channel=0
AUDIO: FIFO primed level=8
AUDIO: PIO/DMA started SM=0
AUDIO: init OK
```

A failure replaces `init OK` with `AUDIO: FAIL stage=... reason=... elapsed_us=...
sdk_rc=...`, a specific condition plus DMA interrupt flags, resource values,
PIO registers and one line per claimed DMA channel.
`milestones=0x1F` proves the enable call was reached; it does not prove measured
clock frequency. Bits 01/02/04/08/10 mean core ready, buffers prepared, DMA started,
FIFO primed and PIO enabled respectively. `completed` counts handled DMA buffers
before the fault. A later fault uses the same once-only dump from the main loop.

Next physical attempt: Compile Project -> Flash Project (SWD), capture UART
115200 8N1 from reset, keep every TX OFF, and check TP1/TP2 (GPIO13/12). Require
48 kHz WS / 1.536 MHz BCLK and idle LED OFF before repeating any SINE/RF test.
If it fails, retain the complete AUDIO block, especially FAIL, milestones and
the register dump. No physical success is claimed for this fix.

Follow-up automated results: production build and all eight firmware suites
passed, including the assembled PIO check, initialization-order checks, every
injected initialization failure and the CTRL_TRIG-vs-AL1_CTRL regression.
The seven existing mux core tests also passed (native direct Zig C++ build;
the initial CMake attempt lacked an archiver). The complete daemon suite was
attempted: **426 passed, 23 skipped, 3 failed**. All three remaining failures
are GStreamer environment limitations: missing `udpsink`, `udpsrc` and
`audiocheblimit` plugins in this Windows environment. The Linux-only integration
tests were skipped. Initial DLL/search-path and subprocess package-path issues
were resolved using process-local settings and ignored temporary helpers, with
no dependency installation or production Python changes. These results do not
constitute physical validation of the audio fix.

Resource/clock failure, DMA bus error, PIO TX stall, missed refill or lack of
DMA progress latches an audio failure. FIFO priming is bounded to 1 ms and the
post-launch boot wait to 100 ms; running progress has a 6 ms watchdog. Runtime
failure stops PIO, drives audio pins LOW and requests DMA abort without waiting
indefinitely. Reset is required; no hidden automatic audio restart occurs.

GET_STATUS reports global ERROR and latched INTERNAL_ERROR for an audio engine
failure; UART names the cause and GPIO27 blinks. Existing TX states remain real:
an audio failure does **not** claim RF OFF or automatically disable RF. Host/QN
control remains available, including OFF. Engine faults do not fabricate SPI
link errors or QN-detected underrun counts. These counters, and absent RDS FIFO
capacity, remain zero. Healthy idle LED is OFF; active RF without critical fault
is steady ON. Firmware does not gate RF ON on audio availability.

GET_INFO, GET_STATUS, QN_READ_REGISTER, QN_WRITE_REGISTER, SET_TX_CONFIG and
SET_TX_ENABLED remain implemented. No command IDs, payload sizes, response
cursor rules or deadlines change. RDS commands, reset and counter reset remain
unimplemented as documented in M8.3; daemon DEGRADED must not be hidden.

## Build and automated checks

Use the existing Pico VS Code **Compile Project -> Flash Project (SWD)** workflow.
The output remains `firmware/fmheadend_rp2354/build/fmheadend_rp2354.elf`.
Equivalent Windows build from the repository root:

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" `
  --build firmware/fmheadend_rp2354/build --parallel 4
```

Native tests need a C compiler and Python 3 for the opcode timing test:

```sh
cmake -S firmware/fmheadend_rp2354/tests -B firmware/fmheadend_rp2354/build/tests
cmake --build firmware/fmheadend_rp2354/build/tests
ctest --test-dir firmware/fmheadend_rp2354/build/tests --output-on-failure
python firmware/fmheadend_rp2354/tests/test_i2s_timing.py --header firmware/fmheadend_rp2354/build/i2s_audio.pio.h
```

If Python is not on PATH, configure `-DPython3_EXECUTABLE=/path/to/python`;
otherwise CMake runs the seven C suites and the Python test must be run separately.

Results on Windows:

- Production Pico SDK 2.3.1 / GCC 15.2.1, pico2 / rp2350-arm-s Debug: build passed,
  ELF/BIN/UF2 generated, host address 0x62.
- Eight CTest suites passed with native Zig/Clang and Python 3.12. Existing
  M8.1–M8.3 bus, 74-case boot, framing, register, RF/failure and timing tests remain.
- New tests cover sine amplitude/frequency, all 256 lane masks, PCM packing,
  phase across DMA buffers, source changes, intentional silence and real faults.
  The actual engine runs against fake DMA/PIO registers through 24 consecutive
  buffers and resource, startup, stall, bus-error and late-refill failures.
- The PIO instruction test also passed against **pioasm's generated header**:
  uniform edges, left/right polarity, one-bit delay, sample order and eight lanes.
- Linux RP client/audio regression tests: **200 passed, 1 skipped** (Linux-only
  i2c-dev test on Windows), two existing dependency deprecation warnings.
  Initial sandbox temp-directory access errors were resolved by the authorized
  rerun; no dependencies were installed.

These are compilation and simulated tests, not measurements of the PCB.

## First physical test: clocks before RF

Keep the existing terminated RF bench setup. Use one Pi control client at a time.
Windows is authoritative. To test the uncommitted smoke-tool update without
editing the Pi Git clone, copy a temporary Windows snapshot:

```powershell
$benchTag = Get-Date -Format 'yyyyMMdd-HHmmss'
$benchArchive = "tmp/m84-bench-$benchTag.tar"
tar -cf $benchArchive --exclude=__pycache__ --exclude=*.egg-info daemon firmware/fmheadend_rp2354/tools
scp $benchArchive "fmheadend:/tmp/m84-bench-$benchTag.tar"
Write-Output "Pi archive: /tmp/m84-bench-$benchTag.tar"
```

On Pi, substitute that STAMP and use the existing M8.3 Python environment:

```sh
bench=$(mktemp -d /tmp/fmheadend-m84.XXXXXX)
tar -xf /tmp/m84-bench-STAMP.tar -C "$bench"
cd "$bench"
PY=/home/fmheadend/venvs/fmheadend/bin/python
"$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cid
```

If the M8.3 test environment was elsewhere, set PY to that existing interpreter.
No new service, dependency or Pi system/I2C configuration is needed.

1. Compile/flash the production ELF over SWD. Open UART GPIO28/29 at 115200 8N1,
   no flow control. Require eight QN successes, RF OFF, LED OFF, firmware 0.8.4,
   `AUDIO: RUNNING; 48000 Hz stereo S16; BCLK=1536000 Hz; DATA=GPIO4..11`, and
   host address 0x62. The existing `V1 M8.3 QN CONTROL` UART label identifies the
   unchanged control implementation, not the firmware version.
2. **Before any RF ON**, scope TP1/WS and TP2/BCLK: 48 kHz and 1.536 MHz, 16
   rising clocks per WS half-cycle. Check at the QN pins too. All DATA lanes must
   be zero with all TXs OFF; clocks must remain present indefinitely. Capture
   edges to check duty cycle and no extra clock at the WS boundaries. Zero data
   alone cannot prove MSB alignment; decode the sine in the next test as well.
3. Run the CID command above: eight end-to-end TX0..TX7 reads, expected board CID2
   0x11, firmware 0.8.4. No RF commands are sent.
4. Once the clock checks pass, explicitly run:

   ```sh
   "$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cycle \
     --tx 0 --frequency-khz 98100 --attenuation-db 22.5 --mode SINE --hold-seconds 20
   ```

   The tool verifies OFF, configures without enabling, holds OFF, enables for
   20 seconds, then sends OFF in a finally block. Require OFF -> ON -> OFF in
   GET_STATUS and LED OFF -> steady ON -> OFF. UART shows real SET OK responses,
   audio_mode=2, no STUB. Check 98.100 MHz carrier only during ON, audible/measurable
   1 kHz modulation, and DATA0 I2S decoded as identical left/right 48-sample sine,
   peak +/-8231. DATA1..7 stay zero. WS changes at the previous word's LSB; the
   following rising clock carries the new MSB. Inspect several DMA boundaries
   (2.667 ms apart): no missing clock, repeated sample or restarted phase.

## Two-lane and mode-change check

Run this from the same temporary Pi snapshot. It uses the existing client;
the pauses allow scope/analyzer measurements. RF stays on while mode changes
perform M8.3's safe temporary disable/reconfiguration/restore sequence.

```sh
"$PY" - <<'PY'
from time import sleep
from daemon.rp2354.linux_client import Rp2354Client
from daemon.rp2354.client import TxConfig
rp = Rp2354Client('/dev/i2c-1', 0x62)
owned = False
try:
    assert rp.get_info()['rp_firmware_version'] == '0.8.4'
    s = rp.get_status()
    assert s['state'] == 'OK' and all(c['tx_state'] == 'OFF' for c in s['modulators'])
    owned = True
    sine = TxConfig(frequency_khz=98100, rf_attenuation_db=22.5, stereo=True,
        preemphasis_us=50, audio_mode='SINE', rds_enabled=False, gain_mode='AUTO',
        analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False)
    digital = sine.model_copy(update={'frequency_khz':98300, 'audio_mode':'DIGITAL'})
    rp.set_tx_config(0, sine)
    rp.set_tx_config(1, digital)
    rp.set_tx_enabled(0, True)
    rp.set_tx_enabled(1, True)
    for mode in ('SINE', 'DIGITAL', 'SINE'):
        rp.set_tx_config(0, sine.model_copy(update={'audio_mode':mode}))
        s = rp.get_status()
        print(mode, s, flush=True)
        assert s['state'] == 'OK'
        assert [c['tx_state'] for c in s['modulators']] == ['ON','ON'] + ['OFF']*6
        sleep(20)
finally:
    try:
        if owned:
            try:
                rp.set_tx_enabled(0, False)
            finally:
                rp.set_tx_enabled(1, False)
            print(rp.get_status(), flush=True)
    finally:
        rp.close()
PY
```

Require DATA1 silent / no 1 kHz modulation on the 98.300 MHz DIGITAL carrier
while DATA0/98.100 MHz carries sine. Switching TX0 to DIGITAL silences DATA0;
switching back restores sine. Capture BCLK/WS continuously through both config
commands and both OFF commands: no clock interruption. No SPI is required.
FM stereo pilot is not program audio and may remain on a silent DIGITAL carrier.

Repeat status and raw CID reads while capturing audio to check host/QN work
does not interrupt clocks. A known QN fault fixture may be tested as in M8.3:
QN_ERROR/CEN isolation must not stop shared clocks or an unaffected sine lane.
For an optional SWD audio-fault check, clear the claimed audio SM's enable bit
while firmware runs; after the FIFO fills and DMA stops advancing, expect
`REFILL DEADLINE`, global ERROR, blink and actual RF state retained. Reset after
fault tests. A failed OFF/transport command cannot prove RF absent: reset the RP
and verify carrier absence. Record traces, UART, board revision and ELF hash.

## Files and remaining work

Added `audio_engine.c/.h`, `audio_samples.c/.h`, `i2s_audio.pio`; updated board,
main, QN state/source notification, host integration/version and CMake in the
production project. Added sample/engine/PIO tests and DMA/PIO fakes; extended
existing boot/QN/host tests. Updated the smoke tool, production README, the SINE
implementation note in protocol documentation and this document. The pre-existing
dirty Linux test file and editor settings were preserved.

No SPI receiver/demultiplexer, Linux audio integration, resampling, gain DSP,
RDS FIFO/scheduler, QN underrun polling, WebUI, automatic RF rotation or separate
firmware was added. Future SPI PCM replaces DIGITAL silence; RDS status remains
planned for polling, not QN_INT. No M8.5 work or new protocol deviation.
