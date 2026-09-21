# M8.6A: RP RDS FIFO transport

Production firmware **0.8.6a**, protocol **V1**, host I2C **0x62**. This adds RAM
transport for Linux-generated RDS groups. **No groups are sent to a QN8007B.**
The M5 Linux encoder, scheduler, wire codec and `Rp2354Client` need no changes.
QN control, RF behavior, SPI's 4100-byte frame, continuous I2S and SINE are unchanged.

## Commands and state

| Command | Request | Successful response |
| --- | --- | --- |
| RDS_PUSH `0x05` | TX0..7; count 1..31, then count groups of four LE u16 blocks | Empty ACK; all groups appended in order |
| RDS_CLEAR `0x06` | TX0..7; zero payload | Empty ACK; only that FIFO reset |
| GET_RDS_FIFO_FREE `0x0B` | Target `0xFF`; zero payload | Exactly 16 bytes: eight LE u16 free-group counts |
| GET_STATUS `0x04` | Existing request | Existing **130-byte** payload, live `rds_fifo_free` fields |

The operator approved preserving the checked-in 130-byte status layout (including
`input_saturated`); the original task's 122-byte description was outdated. The
channel record remains 15 bytes following the 10-byte global prefix; free capacity
is at channel offset 11. RDS underrun fields remain zero because no consumer exists.

Invalid envelope/count/payload returns INVALID_ARGUMENT; an invalid target returns
INVALID_TARGET. Insufficient space returns BUSY and changes neither data nor
indices. The existing command/log backpressure can also return BUSY without
execution. Error ACKs have no payload. PUSH never appends partially or overwrites
queued groups. CLEAR resets both indices and count, without changing RF,
configuration, counters or another channel. Transport works independently of
configuration, `rds_enabled`, RF enable and QN boot readiness.

FIFOs start empty on hardware reset. There is no periodic removal, no artificial
underrun, and no error merely because a queue is empty. Changing TX configuration
or enabling/disabling RF does not drain or clear it. Accepted groups remain queued
until explicit CLEAR or reset; a running Linux scheduler will eventually exhaust
its available credits. ACK means queued, **not transmitted on air**. Existing
RESET_ERROR_COUNTERS/RP_SOFT_RESET remain unimplemented and are not falsely ACKed.

## Storage, ownership and timing

- **64 groups per TX**, eight independent rings. Each group is exactly 8 bytes:
  4,096 bytes of group storage plus 48 bytes of indices/counts = **4,144 bytes**.
  No heap allocation, copied full-frame buffers or new DMA/PIO resources.
- The core0 I2C STOP handler validates the complete request before mutation. It
  copies at most 31 groups / 248 bytes and publishes the updated count/index after
  the copy. CLEAR is constant-time; free-space queries snapshot eight counts.
- All FIFO access is serialized on core0. IRQ handlers own PUSH/CLEAR/free queries;
  foreground GET_STATUS inserts its snapshot during the existing short interrupt
  mask in `host_protocol_complete_work`, after slow QN status reads have finished.
  Core1 SPI/audio code never accesses these FIFOs.
- `rds_fifo_pop` provides the bounded consumer primitive and is tested for exact
  order/wrap-around, but is **not called by production firmware** in M8.6A. A later
  core0 consumer must mask core0 IRQs while copying/removing a group, restore IRQs,
  then perform QN work. Do not hold the mask during QN transactions. Any future
  cross-core consumer would require additional synchronization.
- RDS commands prepare their response at write STOP, without waiting on foreground
  QN work. They retain the existing **2 ms** Linux pre-read wait and split header /
  payload reads with a preserved cursor. Long QN command waits are unchanged.
  Native tests verify readiness without a foreground poll; physical latency still
  needs the bench check. No I2S restart, clock gating, SPI IRQ work or QN work is
  added to the host IRQ.
- Successful free-space polling is quiet. UART logs empty-to-nonempty and full
  transitions, nonempty CLEARs, and errors through the existing bounded foreground
  log queue. No per-group printing or UART calls inside the I2C IRQ.

## Build and tests

Use the existing production Pico VS Code project: **Compile Project -> Flash
Project (SWD)**. UART remains **115200 8N1**, GPIO28 TX / GPIO29 RX. Boot banner
identifies M8.6A; all eight QNs still boot assigned to 0x21..0x28 with RF OFF.
GPIO27 remains OFF at healthy idle. Output is
`firmware/fmheadend_rp2354/build/fmheadend_rp2354.elf` (also UF2).

Windows commands using the existing SDK/native build:

```powershell
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build --parallel 4
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path (Get-Location) 'tmp/m83-zig-cache'
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/cmake.exe" --build firmware/fmheadend_rp2354/build/native-m83 --parallel 4
& "$env:USERPROFILE/.pico-sdk/cmake/v4.3.4/bin/ctest.exe" --test-dir firmware/fmheadend_rp2354/build/native-m83 --output-on-failure
```

Validation on Windows (2026-09-21): production Pico SDK 2.3.1 / GCC 15.2.1
build succeeded (`pico2`, `rp2350-arm-s`, Debug, host address 0x62). All **16/16**
firmware CTest suites passed, including the preserved M8.1-M8.5 suites. Linux
RDS/RP client/codec/audio tests: **245 passed, 1 skipped** (the real Linux i2c-dev
open-failure test cannot run on Windows). Two existing dependency deprecation
warnings were emitted. No hardware verification was performed.

The Linux tests used the existing isolated test tools, without installing packages:

```powershell
& "$env:USERPROFILE/radioconda/python.exe" tmp/run_m83_pytests.py daemon/tests/test_rds.py daemon/tests/test_rp2354.py daemon/tests/test_rp2354_audio.py -q -ra --basetemp=tmp/m86a-pytest
```

Coverage includes:

- Empty/full capacity, 1/31-group batches, exact LE block preservation including
  the last group of the maximum payload, multiple ordered PUSHes and wrap-around.
- All eight lanes, isolated CLEAR, clean reuse, atomic BUSY with byte-for-byte
  unchanged storage, malformed count/length/targets, bounded backpressure.
- Exact 16-byte free response, split reads, live 130-byte status and zero underruns.
- Hardware-adapter mocks check no QN/UART/audio calls from IRQ, no RF or source
  changes from PUSH/CLEAR even with a SINE TX already ON, and unchanged LED logic.
- All M8.1-M8.5 firmware suites; Linux M5 RDS/client/codec/audio validation suites.
- The Pi smoke tool with a fake client, including cleanup after a failed read.

Changed files under `firmware/fmheadend_rp2354/`:

- New `rds_fifo.c/.h`; `host_protocol.c/.h` for commands, status and version;
  `host_i2c.c` for foreground transition logs.
- `CMakeLists.txt`, `fmheadend_rp2354.c`, `README.md` for build/version documentation.
- New `tests/test_rds_fifo.c`, `tests/test_rds_protocol.c`, `tests/test_rds_smoke.py`;
  updated `tests/CMakeLists.txt`, `tests/test_host_protocol.c`, `tests/test_host_i2c.c`.
- New `tools/rds_smoke.py`; version compatibility update in `tools/qn_smoke.py`.
- Repository docs: this document and an implementation note in
  `docs/rp2354-protocol.md`. Its pre-existing M8.4/M8.5 note is retained.

## First physical Pi smoke (RF OFF)

Keep the existing terminated bench setup. Build/flash M8.6A and reset the RP.
Keep other host-control clients, including `fmheadendd` and its RDS refill loop,
stopped for this exclusive test. The existing SPI mux may keep running; it does
not use these FIFOs. There is no need to enable RF or change QN registers.

After the operator commits and pushes from authoritative Windows with `git push pi`,
update the Pi test clone and run:

```bash
ssh fmheadend
cd ~/fmheadend
git pull --ff-only
PY=/home/fmheadend/venvs/fmheadend/bin/python
"$PY" firmware/fmheadend_rp2354/tools/rds_smoke.py --device /dev/i2c-1 --address 0x62
```

This invokes the existing `Rp2354Client`. It checks the firmware version and:

1. All eight free counts initially equal 64, status agrees, all RF OFF, no RDS underruns.
2. PUSHes two known groups into TX0: `(1234, ABCD, 8001, 00FF)` and
   `(FFFF, 0000, 55AA, A55A)` (hexadecimal blocks).
3. Checks free counts `[62,64,64,64,64,64,64,64]` and matching GET_STATUS.
4. CLEARs TX0 and checks all eight counts return to 64.
5. Closes the client; on an intermediate failure it attempts TX0 cleanup.

Expected console lines (after GET_INFO):

```text
BOOT: free=[64, 64, 64, 64, 64, 64, 64, 64]
PUSH TX0: 2 known groups: free=[62, 64, 64, 64, 64, 64, 64, 64]
CLEAR TX0: free=[64, 64, 64, 64, 64, 64, 64, 64]
PASS: FIFO counts/status/isolation; no QN RDS transmission or RF command.
```

Expected FIFO UART lines (ordinary INFO/STATUS diagnostics may appear between):

```text
HOST RX: RDS_PUSH TX0 groups=2 free=62 (FIFO transition)
HOST TX: OK payload=0 bytes
HOST RX: RDS_CLEAR TX0 free=64
HOST TX: OK payload=0 bytes
```

There should be no free-query spam, no RDS underruns and no RF/LED state change.
WS=48 kHz and BCLK=1.536 MHz must remain present throughout. Existing M8.5 PCM/SINE
tests remain applicable; `qn_smoke.py` now accepts the M8.6A firmware version.
Wire-level contents/order and wrap-around are verified natively; this FIFO-only
hardware API has no readback/pop command, so the physical smoke checks counts and
isolation rather than claiming over-the-air payload verification.

## Remaining work

QN RDS status polling, group consumption/transmission, transmit pacing and actual
consumer underrun behavior remain absent. No Linux scheduling changes, RDS IRQs,
new protocol command, audio work or M8.6B implementation is included. No board test
or commit has been performed as part of this implementation.
