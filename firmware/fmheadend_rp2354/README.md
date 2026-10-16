# Standalone FMheadend RP2354 firmware

Open this directory itself as the Raspberry Pi Pico VS Code project. All source,
build/provisioning scripts, bench helpers and tests needed by this project are
local. The host OTA uploader is a separate application; the firmware build never
imports or invokes it. The OTA interface remains a `.bin` with adjacent `.bin.json`.

## Build and program (Windows 11)

Install the Pico extension toolchain in its normal user directory: Pico SDK 2.3.1,
ARM GCC 15_2_Rel1, CMake 4.3.4, Ninja 1.13.2, picotool 2.3.1 and OpenOCD
0.12.0+dev. Python 3.11 or later must be discoverable by CMake. No host repository
or host virtual environment is required. From this project directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rp2354_build.ps1
```

**Compile Project** runs this same operation. It builds all eight TX populations, factory and OTA, from the
same source, validates both manifests and generates the partition table:

```text
build/factory/fmheadend_rp2354_tx{1..8}.bin
build/factory/fmheadend_rp2354_tx{1..8}.bin.json
build/ota/fmheadend_rp2354_tx{1..8}.bin
build/ota/fmheadend_rp2354_tx{1..8}.bin.json
build/partitions.bin
```

Each image has an isolated target/build header; the build ID includes TX count
and packet frame count. Manifests retain the existing uploader-compatible schema.
Provisioning selects `build/factory/fmheadend_rp2354_tx8.bin` by default.

`firmware_config.h` defines `TX_COUNT` (1..8, default 8) and
`FRAMES_PER_PACKET` (1..128, default 128). CMake builds every TX count and accepts
`-DFRAMES_PER_PACKET=64` (use a separate build directory for different packet
sizes). The two 4096-byte output buffers and four SPI slots stay statically sized;
only the active DMA count, PCM stride and refill timing vary. Missing TX lanes
are zero-filled. GET_INFO reports the actual population; existing fixed-size
status/FIFO replies retain eight entries, with absent entries inactive/empty.

The unmodified host supports only TX8/128. Other populations need matching host
SPI lengths and GET_INFO support; smaller packets also need matching send cadence.
No hardware timing qualification is implied for small packet sizes.

Factory: ROM 1.0, non-TBYB. OTA: ROM 1.1, TBYB. Manifests contain the embedded
build ID, ROM version, image size, TBYB and complete SHA-256. Source/build settings
determine the build ID. A copied CMake cache is automatically reconfigured for
the new project root by the PowerShell entry point. Generated build directories
can also be omitted when copying the project.

**Flash Pico Project (SWD)** delegates to **Flash RP2354 Factory (SWD)**. Both
build/validate and immediately start provisioning without interactive questions.
They identify the RP2350-family device and autodetected 2 MiB flash, erase all
sectors, write the partition table and factory image, then compare a complete
physical flash readback byte for byte. No target CRC algorithm is used.
An unavailable SWD target fails without an automatic hardware reconnect loop.
The local Tcl-server startup wait is not a retry of the hardware connection.

Provisioning uses the Python selected by CMake. The hardware-free preflight is:

```powershell
python tools/rp2354_provision.py --build-dir build --dry-run
```

Readback and OpenOCD logs are retained in `build/`. A post-verification reset
failure is reported separately: flash contents may be verified while normal
boot is still unconfirmed. Release BOOTSEL and reset manually if required by
the board. Agent-initiated hardware work follows [AGENTS.md](AGENTS.md); this
does not introduce a confirmation prompt into user-invoked tasks.

**Debug** configurations attach without loading. Select the symbols for the
running factory or OTA target via the Pico extension. Flat ELF/UF2 loading is
blocked: the native 0x10000000 runtime origin maps to an A/B slot at boot, while
physical 0x10000000 holds the partition table. Never replace the Flash task with
a generic `program ELF` command.

## Firmware boundaries

Firmware 0.8.6b uses protocol V1, host address 0x62, two-stage READ and CRC16.
Host bus configuration remains 50 kHz. The IRQ services the hardware peripheral;
bounded foreground work handles QN control and flash operations. Safe boot leaves
RF OFF. Normal UART is 115200 8N1 on GPIO28/29, GPIO27 indicates status.

Flash offsets: table 0x000000, A 0x004000, B 0x0F4000, journal 0x1FE000.
Each application slot is 960 KiB. Native ROM A/B selection, TBYB trial and explicit
buy are used; no custom bootloader. Equal/older version activation follows the
ROM rules. Successful CONFIRM/ABORT returns to normal operation by ACK-gated
reboot. The host must verify the resulting slot/build/finalization state.

Audio is 48 kHz stereo, 16-bit I2S: BCLK 1.536 MHz and WS 48 kHz. Core1 owns
SPI/I2S DMA and PIO. Missing digital samples produce silence. Audio/RDS never
implicitly enables RF. RDS is a bounded FIFO transport; the bench helpers supply
test groups, not application scheduling policy.

## Offline tests and optional bench tools

With a native C compiler (not ARM GCC), CMake and Python:

```text
cmake -S tests -B build/tests
cmake --build build/tests
ctest --test-dir build/tests --output-on-failure
python -m pytest tests/test_rp2354_provision.py tests/test_rp2354_partitions.py
```

Set `PICOTOOL` to the installed executable for the offline partition tests.
Those tests only create/inspect local files. Pytest is required for these two
test modules; the existing CTest Python checks use the standard library.

The optional Linux bench tools in `tools/` use the local `bench_support` snapshot
for their existing wire codec, Linux client, configuration validation and RDS
encoder. They do not import the external daemon. Install
`tools/requirements-bench.txt` for tools using TxConfig (Pydantic 2). Build and SWD
provisioning need only standard-library Python. Bench tools can operate real
hardware: do not run them as part of ordinary offline validation.

Known hardware findings from the earlier workflow validation: full SWD readback
and OTA in both directions succeeded, but OpenOCD normal reset failed on that
board and repeated I2C `80 80` header/CRC errors required recovery. This structural
refactor changes neither behavior nor timing and is not a claim to fix those faults.
