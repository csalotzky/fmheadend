# M8.5: Pi SPI audio reception and eight-way PCM demultiplexing

Production firmware **0.8.5**, host I2C **0x62**, protocol **V1**. M8.1 safe boot,
M8.3 QN operations/response waits and M8.4 continuous I2S are retained. No QN
register mapping changes. No commit, flash, Pi modification or physical test was
performed by the implementation agent. The operator must accept this on hardware.

## Pins and peripheral choice

Checked the actual `firmware/fmheadend_i2ctest/fmheadend.pdf`, PCB1.0 sheets 1/4
and 4/4, against `board.h`. No discrepancy found.

| Signal | Pi SPI0 / header | RP GPIO |
|---|---|---|
| MOSI | BCM10 / 19 | 0 |
| CE0, active LOW | BCM8 / 24 | 1, external 10 kOhm pull-up |
| SCK | BCM11 / 23 | 2 |
| MISO | BCM9 / 21 | 3, left as input; no response protocol |

PIO1 receives mode 0, MSB first, at the requested **25 MHz**. One SM and one DMA
channel are claimed dynamically; boot prints their actual numbers. PIO0 and its
two I2S DMA channels remain independent. The hardware PL022 peripheral requires
`clk_peri >= 12 * slave SCK`: 150 MHz only supports 12.5 MHz slave SCK. Therefore
this implementation uses PIO on the same PCB SPI pins. See the
[RP2350 datasheet](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf),
section 12.3.5, p.1050. No overclocking or pin remapping.

The five-instruction receiver waits for CS LOW, then loops over SCK HIGH, IN,
bit-counter decrement and SCK LOW. At 150 MHz there are six SM cycles per 25 MHz
bit; the loop needs four instructions including the two waits. Both input
synchronizers stay enabled. Left shift/autopush every eight bits puts each byte
in the FIFO's low byte. Byte-sized DREQ-paced DMA writes it directly to SRAM.
X counts all sampled bits so a trailing partial byte is rejected too.

## Frozen frame, buffers and ownership

One CS assertion must contain **4100 bytes / 32800 bits**:

| Offset | Contents |
|---|---|
| 0 | uint32 little-endian frame counter |
| 4, 516, 1028, 1540, 2052, 2564, 3076, 3588 | TX0..7 respectively; 512 bytes each |

Each TX block is 128 `(int16_le left, int16_le right)` pairs at 48 kHz. No magic,
length, version, CRC, padding or response bytes. Linux muxd is unchanged: one
`SPI_IOC_MESSAGE(1)` per frame at nominal 375 frames/s, including silent frames.

- Core 0 initializes receiver resources after QN safe boot, before audio init.
- Core 1 arms reception before starting I2S; its GPIO CS rise/fall IRQ handles
  boundaries. No DMA IRQ, byte IRQ, polling of individual bytes, UART or QN work.
- CS rise stops the SM, disables DMA through **AL1_CTRL**, and aborts outstanding
  transfers with a 20 us timeout. At most eight FIFO entries are drained. Byte
  count, bit count and hardware flags must all pass before publication.
- DMA captures up to **4101 bytes**, allowing oversized transactions to be
  identified. Longer transfers can also cause an RX FIFO stall; either rejects
  the entire frame. CS LOW exceeding 8 ms is rejected by core1 foreground polling.
- Four aligned 4104-byte slots occupy **16,416 bytes**: at most **three queued or
  borrowed frames plus one capture slot**. No frame copies and no per-TX queues.
- Core1 refill briefly masks its CS IRQ to borrow/release a frame. The slot stays
  owned until packing finishes; reception cannot overwrite it. PCM packing runs
  with IRQs enabled. Core 0 publishes a single atomic word containing both lane
  masks; core 1 snapshots it once per refill. Error/event atomics cross to core 0.
- The existing two 128-frame I2S buffers remain **8 KiB**. Four samples' bit times
  are packed into each word, with the eight lanes represented in every byte.
  Exhaustive tests check all 65,536 sample bit patterns. No malloc/free.

The existing I2S DMA start/chaining and non-triggering AL1_CTRL rearm are retained.
BCLK **1.536 MHz**, WS **48 kHz**, 16-bit left/right slots, I2S one-bit WS lead and
MSB-first samples are unchanged. Shared clocks never depend on modes, RF or SPI.

## Playback and latency policy

Playback waits for **two complete frames** (256 samples / 5.333 ms of PCM), then
consumes exactly one queued frame per 128-sample refill. Adjacent frames are never
repeated, reordered or resampled. The queue holds at most 384 samples / 8 ms.
The I2S buffers add a pipeline of up to 256 samples / 5.333 ms, plus FIFO latency
under 0.03 ms. Budget **up to about 13.4 ms of RP buffering** in normal operation;
this excludes Linux producer queues, scheduling and the **1.312 ms** SPI transfer.
Actual latency depends on arrival/refill phase. A queued head older than **20 ms**
is discarded as stale; this is an exceptional recovery guard, not target latency.

- **Configured DIGITAL:** its own SPI L/R samples, **including with RF OFF** as
  explicitly approved for the no-RF bench test. Unconfigured/unavailable lanes
  remain zero. No fresh frame means zeros, never replay of the previous block.
- **SINE:** M8.4's continuous 1 kHz, -12 dBFS local phase, only while successfully
  configured and RF ON. It overrides SPI on its lane.
- **ANALOG:** zero I2S DATA; incoming SPI does not change QN analog operation.

All SPI frames are consumed even if no DIGITAL lane currently uses them. Source
changes apply at refill boundaries; already packed I2S data may persist for about
5.4 ms. A bad frame flushes older queued blocks and restarts two-frame prefill.
Already playing/packed **valid** PCM can finish; invalid/partial PCM is never used.
After starvation, later valid frames automatically restart playback after prefill.
RF is neither disabled nor restarted by any transport event.

## Error accounting and diagnostics

The existing GET_STATUS `rp_spi_audio_link_error_count` is the sole cumulative
transport error counter (u32, saturates at UINT32_MAX, resets at RP boot):

- One increment per rejected length/partial-bit/hardware-error transaction.
- One per counter discontinuity: forward gap, duplicate or backward counter.
  `previous + 1` wraps modulo 2^32. The offending frame is discarded and becomes
  the new counter baseline; the following sequential frame can be accepted.
  This also permits recovery after muxd restarts at zero. An invalid header
  clears the baseline, avoiding a second gap report for the same corrupt frame.
- One per queue overflow (newest rejected; queued audio flushed on next take),
  stale queue, or established playback starving. Continued absence does not
  increment on every refill. Before the first playback, absence is normal.
- Receiver initialization/resource failure increments once and logs the stage;
  DIGITAL stays silent. Missed CS edges, unavailable DMA and stuck CS are counted.

Valid all-zero PCM, producer disconnect, TX OFF and ANALOG/SINE selection never
increment it by themselves. Producer loss leaves muxd sending valid silent frames.
Stopping muxd after playback does produce one stream-starvation error. A malformed
frame can produce several event labels but only one increment for that rejection.

These transport events do not fabricate QN underruns, RDS activity, active global
critical faults or RF OFF. GPIO27 keeps M8.3/M8.4 semantics. An actual I2S engine
fault still reports INTERNAL_ERROR and blinks. UART is 115200 8N1 on GPIO28/29.

Boot includes (resource numbers may vary):

```text
SPI AUDIO: init OK PIO1 SM=0 DMA=0 MOSI=0 CS=1 SCK=2 MISO=unused; mode0 25MHz; slots=4 queued=3 prefill=2 frame=4100
AUDIO: ...
AUDIO: init OK
```

Core0 drains bounded event summaries, at most once/second, through the existing
nonblocking UART queue: `first valid frame`, `stream lost`, `stream recovered`,
`invalid length`, `counter gap`, `duplicate`, `backward counter`,
`receive/DMA error`, `queue overflow`, followed by cumulative `errors=N`.
No per-frame printing. Multiple events may share a line.

## Build and automated verification

Normal Pico VS Code **Compile Project -> Flash Project (SWD)** remains usable.
The agent built the production ELF/UF2 using SDK 2.3.1, GCC 15.2.1, pico2 /
rp2350-arm-s, Debug, address 0x62. On this Windows development machine:

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build --parallel 4
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path (Get-Location) 'tmp/m83-zig-cache'
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build/native-m83 --parallel 4
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/ctest.exe" --test-dir firmware/fmheadend_rp2354/build/native-m83 --output-on-failure
```

Results:

- Production build passed. **13/13 native firmware/PIO/tool tests passed**, with
  all M8.1–M8.4 cases retained. New tests cover frame sizes/offsets/LE decoding,
  counters/wrap, queue ownership, starvation/recovery, CS/DMA/resource failures,
  partial bytes, DIGITAL/SINE/ANALOG selection and pipeline continuity. Host
  tests verify actual SPI count serialization without a critical fault.
- PIO simulation passes at 25 MHz over ten input edge phases, complete and partial
  frames; generated pioasm opcodes checked. Existing I2S opcode/timing test passes.
- Unchanged C++ mux core rebuilt: **7/7 tests passed**, including frame/counter.
- Focused Linux RP client/codec tests: **200 passed, 1 Linux-only skip**.
- Full Python suite: **426 passed, 23 skipped, 3 failed**. The three failures are
  existing Windows GStreamer availability limitations (`udpsink`, `udpsrc`,
  `audiocheblimit`), also recorded during M8.4. No Linux/backend code changed.
  Linux socket/spidev execution is unavailable here (no WSL installation); run
  the following CTest on the Pi to include its socket integration test.

Compilation and software models are **not physical verification**.

## First physical test: no RF

After reviewing and committing on Windows yourself, deploy with `git push pi`;
on Pi use `git pull --ff-only`. Do not edit/commit on the Pi. Flash the production
firmware through the normal SWD workflow. Use the terminated RF bench setup.
Stop other controllers/mux instances for this manual bench session.

1. Require eight QNs ready, RF OFF, LED OFF, `SPI AUDIO: init OK` and
   `AUDIO: init OK`. Before sending any PCM, scope GPIO12 BCLK = 1.536 MHz and
   GPIO13 WS = 48 kHz. All DATA lanes initially zero. Clocks must stay present.
2. On Pi, verify existing device access and buffer size; no automatic boot edits:

   ```sh
   cd ~/fmheadend
   git pull --ff-only
   PY=/home/fmheadend/venvs/fmheadend/bin/python
   ls -l /dev/spidev0.0 /dev/i2c-1
   cat /sys/module/spidev/parameters/bufsiz
   test "$(cat /sys/module/spidev/parameters/bufsiz)" -ge 4100
   "$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cid
   cmake -S muxd -B "$HOME/build/fmheadend-muxd-m85" -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$PY"
   cmake --build "$HOME/build/fmheadend-muxd-m85" -j2
   ctest --test-dir "$HOME/build/fmheadend-muxd-m85" --output-on-failure
   ```

   Use the existing Python environment if its location differs. If spidev is
   missing or bufsiz <4100, stop and follow [M3 prerequisites](milestone3.md).
   `spidev.bufsiz=8192` is preferred. Never split a frame into separate CS pulses.

3. Configure TX0 DIGITAL while all RF remains OFF:

   ```sh
   "$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 configure \
     --tx 0 --frequency-khz 98100 --attenuation-db 22.5 --mode DIGITAL
   mkdir -p "$HOME/run/fmheadend-m85"
   "$HOME/build/fmheadend-muxd-m85/fmheadend-muxd" --transport spidev \
     --runtime-dir "$HOME/run/fmheadend-m85" --spi-device /dev/spidev0.0 \
     --spi-clock-hz 25000000 --spi-mode 0
   ```

   Leave muxd in this terminal. Require `first valid frame ... errors=0` on RP
   UART, even before a PCM producer connects. SPI success in Linux alone does
   not prove reception. Probe SCK and CS: mode0, nominal 25 MHz, 32800 clocks
   per assertion, approximately 1.312 ms LOW / 1.355 ms HIGH at nominal cadence.

4. In another Pi terminal, read back spidev settings without transferring data:

   ```sh
   cd ~/fmheadend
   PY=/home/fmheadend/venvs/fmheadend/bin/python
   "$PY" - <<'PY'
   import os, fcntl
   fd = os.open('/dev/spidev0.0', os.O_RDONLY)
   try:
       for name, nr, size, expected in [('mode',1,1,0), ('lsb_first',2,1,0),
                                        ('bits',3,1,8), ('max_hz',4,4,25000000)]:
           value = bytearray(size)
           fcntl.ioctl(fd, (2<<30)|(size<<16)|(ord('k')<<8)|nr, value, True)
           actual = int.from_bytes(value, 'little')
           print(name, actual)
           assert actual == expected
   finally:
       os.close(fd)
   PY
   "$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 status
   "$PY" firmware/fmheadend_rp2354/tools/pcm_smoke.py \
     --runtime-dir "$HOME/run/fmheadend-m85" --tx 0 --pattern constant --seconds 60
   ```

   These read ioctls follow the Linux
   [spidev UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/spi/spidev.h).
   Readback is the requested limit, not a measurement of actual SCK.
   Decode DATA0/GPIO4 as signed 16-bit stereo I2S: **L=4660 (0x1234),
   R=-9029 (0xDCBB)**. DATA1..7 must stay zero. Verify no carrier, all TX OFF,
   LED OFF, uninterrupted BCLK/WS and SPI count still zero. Ctrl-C stops only
   the producer; muxd keeps valid silence frames flowing.

## RF and independent-lane tests

Keep muxd running. Start the tone producer in a separate terminal:

```sh
"$PY" firmware/fmheadend_rp2354/tools/pcm_smoke.py \
  --runtime-dir "$HOME/run/fmheadend-m85" --tx 0 --pattern tone --seconds 180
```

Then explicitly perform the existing OFF/ON/OFF bench cycle:

```sh
"$PY" firmware/fmheadend_rp2354/tools/qn_smoke.py --address 0x62 cycle \
  --tx 0 --frequency-khz 98100 --attenuation-db 22.5 --mode DIGITAL --hold-seconds 60
```

The tool holds RF OFF for 60 s, ON for 60 s, then sends OFF in a finally block.
Require 98.100 MHz carrier only during ON, 1 kHz modulation, identical L/R PCM
peak +/-8231, correct status and LED OFF -> ON -> OFF. During ON, stop the
producer: DATA becomes zero after producer/mux/RP buffered PCM drains, modulation
disappears but carrier stays ON. Restart the producer and require recovery
without a carrier restart or SPI link error. A stereo pilot is not program audio.

For two channels, stop the first producer and run it with **`--tx 0 --tx 1`**.
TX0 supplies 1 kHz, TX1 1.5 kHz. In the control terminal:

```sh
"$PY" - <<'PY'
from daemon.rp2354.linux_client import Rp2354Client
from daemon.rp2354.client import TxConfig
rp = Rp2354Client('/dev/i2c-1', 0x62)
owned = False
try:
    s = rp.get_status()
    assert s['state'] == 'OK' and all(c['tx_state']=='OFF' for c in s['modulators'])
    owned = True
    config = TxConfig(frequency_khz=98100, rf_attenuation_db=22.5, stereo=True,
        preemphasis_us=50, audio_mode='DIGITAL', rds_enabled=False, gain_mode='AUTO',
        analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False)
    rp.set_tx_config(0, config)
    rp.set_tx_config(1, config.model_copy(update={'frequency_khz':98300}))
    input('RF OFF: decode DATA0=1 kHz, DATA1=1.5 kHz; Enter to enable both: ')
    for tx in (0,1): rp.set_tx_enabled(tx, True)
    print(rp.get_status(), flush=True)
    input('Measure 98.1/98.3 MHz carriers and separate tones; Enter for SINE override on TX1: ')
    rp.set_tx_config(1, config.model_copy(update={'frequency_khz':98300,'audio_mode':'SINE'}))
    input('DATA1 now local 1 kHz; DATA0 unaffected, clocks continuous. Enter for DIGITAL: ')
    rp.set_tx_config(1, config.model_copy(update={'frequency_khz':98300}))
    input('DATA1 back to SPI 1.5 kHz; Enter to disable both: ')
finally:
    try:
        if owned:
            try: rp.set_tx_enabled(0, False)
            finally: rp.set_tx_enabled(1, False)
            print(rp.get_status(), flush=True)
    finally: rp.close()
PY
```

M8.3 reconfiguration temporarily disables/restores that QN's RF; shared I2S
clocks must never stop. Configured DIGITAL DATA may continue after RF OFF.
Repeat with other TX pairs to check all eight physical lane routes. Verify
ANALOG configuration leaves that DATA lane zero despite incoming SPI, while
another DIGITAL/SINE lane continues. Use only board-representable analog gain
settings from M8.3; no analog gain mapping changed here.

## Recovery checks and remaining risks

With RF OFF, stop muxd, observe one `stream lost` increment and DIGITAL silence,
then restart the same launch command. Require `stream recovered`, sequential
playback, continuous clocks and no RP reset. A backward counter on restart is
also counted if a previous counter baseline exists.

With muxd stopped and RF OFF, this deliberately sends one **short** transaction:

```sh
"$PY" - <<'PY'
import os
fd = os.open('/dev/spidev0.0', os.O_WRONLY)
try: assert os.write(fd, bytes(4099)) == 4099
finally: os.close(fd)
PY
```

Require `invalid length`, one additional error, silence and successful recovery
when muxd restarts. SPI settings persist from the muxd launch; do not run another
SPI writer concurrently. Native tests cover oversize, partial-bit, gap, duplicate,
backward, DMA timeout and overflow cases without risking live RF.

Unresolved physical acceptance items:

- Measure setup/hold, actual SCK and minimum CS HIGH interval. Re-arm is performed
  by a bounded CS IRQ (abort alone limited to 20 us), not an autonomous back-to-back
  transaction sequencer. Nominal idle is about 1.355 ms. An edge arriving before
  re-arm is discarded/counted; no guarantee for arbitrarily short CS gaps.
- Exercise all eight DIGITAL lanes plus I2C/QN commands for an extended run.
  Verify no I2S refill deadlines, DMA/PIO stalls or unintended SPI errors. Native
  tests cannot establish on-board bus contention or ISR/refill timing margin.
- Linux cadence and RP crystal are independent. This milestone has no resampler
  or clock feedback. Sustained mismatch eventually causes bounded overflow or
  starvation, a counted discontinuity, silence and re-prefill. It never grows
  the queue indefinitely or changes carrier state.
- No CRC: same-length corruption with a plausible counter can remain undetected.

Changed production modules: board/build/version/main, `spi_audio.*`,
`spi_stream.*`, audio engine/sample packing, QN source notifications and host
status/UART integration. Added/extended native/PIO/PCM tests and bench tools;
updated README and the V1 implementation note. Diagnostic firmware, Linux muxd,
Linux daemon, QN register mapping, I2S PIO and Git remotes remain unchanged.
Existing untracked editor settings were preserved.

RDS FIFO/scheduling, resampling, AGC/normalization, WebUI and automatic deployment
remain absent. This completes the M8.5 implementation only; physical acceptance
and committing belong to the operator.
