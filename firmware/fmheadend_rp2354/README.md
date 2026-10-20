# FMheadend Controller firmware (RP2354)

Firmware for the FMheadend Controller, based on the RP2354 with **2 MiB flash** (`rp2354-qn8x-v1`). It controls **1–8 physical FM transmitters**, receives PCM over SPI, handles host-generated RDS groups and supports A/B firmware updates. RF is OFF at startup; unavailable PCM is replaced with silence.

- Controller protocol: **version 1**
- Firmware application version: **1.0.0** (`firmware_config.h`)
- Standard host audio format: **128 stereo frames per TX per SPI packet**, 48 kHz

This directory can be built, tested and provisioned independently of the main FMheadend repository. The Linux I²C firmware updater is supplied separately in the host repository.

## 1. Build

### Prerequisites

The supplied PowerShell workflow uses the Raspberry Pi Pico VS Code extension's Windows toolchain:

| Tool | Configured version |
|---|---|
| Pico SDK / picotool | 2.3.1 |
| ARM GCC | 15_2_Rel1 |
| CMake / Ninja | 4.3.4 / 1.13.2 |
| OpenOCD (SWD/debug) | 0.12.0+dev |
| Python | 3.11+ |

Open **this firmware directory** as the VS Code workspace. **Compile Project** runs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rp2354_build.ps1
```

Or, with Pico SDK, ARM compiler, picotool and Ninja already configured:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

Set `PICO_SDK_PATH`, `PICO_TOOLCHAIN_PATH` and, if needed, `picotool_DIR` for a custom toolchain. Use a fresh `build/` directory when changing machines or toolchains. **Building does not program the device.**

### Output files

A standard build generates **16 firmware images**: factory and OTA images for each physical TX count `N = 1..8`.

```text
build/factory/fmheadend_rp2354_txN.bin
build/factory/fmheadend_rp2354_txN.bin.json
build/ota/fmheadend_rp2354_txN.bin
build/ota/fmheadend_rp2354_txN.bin.json
build/partitions.bin
```

Each binary must stay with its matching `.bin.json` manifest. The filename determines the intended TX population; the manifest does **not** independently encode the TX count. Matching ELF files are generated for debugging.

### Build options

| Setting | Default | Description |
|---|---|---|
| `TX_COUNT` | 8 in `firmware_config.h` | Standard build generates separate TX1–TX8 targets |
| `FRAMES_PER_PACKET` | 128 | Stereo frames per TX; standard host requires 128 |
| `DEBUG_UART_ENABLED` | OFF | Optional 115200 8N1 debug output |
| `FMHEADEND_HOST_I2C_ADDRESS` | `0x62` | Controller 7-bit I²C address (`0x08..0x77`) |

The standard host sends `4 + 512*N` SPI bytes per packet at **375 packets/s**. A different `FRAMES_PER_PACKET` value requires a matching host sender; it is not negotiated at runtime.

Enable or disable UART diagnostics in an existing build:

```sh
cmake -S . -B build -DDEBUG_UART_ENABLED=ON
cmake --build build --parallel
# To disable again:
cmake -S . -B build -DDEBUG_UART_ENABLED=OFF
cmake --build build --parallel
```

UART output uses the firmware's designated debug connection; confirm the board's wiring before enabling it.

## 2. Initial installation (SWD)

> **Destructive:** provisioning erases the **entire 2 MiB flash**, including both application slots and update journals. The supplied provisioning script installs **TX8 factory firmware only**. Do not use it on a TX1–TX7 board.

Connect a suitable Debug Probe and put the RP2354 into **BOOTSEL**. After building, optionally validate artifacts without accessing hardware:

```powershell
python tools/rp2354_provision.py --build-dir build --dry-run
```

To erase and program the device (starts **without a confirmation prompt**):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rp2354_build.ps1 -Provision
```

The VS Code **Flash RP2354 Factory (SWD)** task performs the same operation.

The script checks the chip family and 2 MiB flash size, then:

| Physical address | Operation |
|---|---|
| `0x10000000` | Write `build/partitions.bin` |
| `0x10004000` | Write `build/factory/fmheadend_rp2354_tx8.bin` |

It verifies the **entire flash by readback** before requesting a cold reset. Results are saved to `build/provision-openocd.log` and `build/provision-readback.bin`.

If readback succeeds but the reset is not acknowledged, release BOOTSEL, reset manually and check Controller status; **do not erase again solely because the reset failed**. Failed readback means provisioning is not verified.

**Never flash the ELF or UF2 as a flat image at `0x10000000`:** that overwrites the partition table. The ROM maps the selected A/B application image to that runtime address; factory firmware is physically installed at `0x10004000`.

## 3. Debugging and tests

The supplied Cortex-Debug configurations **attach without loading firmware**. They default to TX8 factory symbols (`build/factory/fmheadend_rp2354_tx8.elf`); select the ELF matching the running image. Debug attachment may halt the Controller.

Offline native tests use fake hardware. With a host C compiler, CMake and Python:

```sh
cmake -S tests -B build/tests-debug -DCMAKE_BUILD_TYPE=Debug
cmake --build build/tests-debug
ctest --test-dir build/tests-debug --output-on-failure

cmake -S tests -B build/tests-release -DCMAKE_BUILD_TYPE=Release
cmake --build build/tests-release
ctest --test-dir build/tests-release --output-on-failure
```

Use a single-configuration generator (such as Ninja or Unix Makefiles). Additional offline Python tests:

```sh
python -m pytest tests/test_rp2354_provision.py tests/test_rp2354_partitions.py
```

Install `pytest` separately. Set `PICOTOOL` to picotool 2.3.1 for partition tests; otherwise those checks skip. The optional bench tools under `tools/` may access hardware and enable RF; **do not run them as offline tests**. Tools requiring Pydantic models use `tools/requirements-bench.txt`.

## 4. I²C firmware updates

Once the board has a valid A/B installation, subsequent updates use the host repository's **`tools/rp2354_flash.py`**, not the SWD provisioning script. The uploader requires the host Controller client, firmware artifact validator and installed `fmheadendd` package metadata.

Choose the matching `build/ota/fmheadend_rp2354_txN.bin` and `.bin.json`. Stop other Controller clients before starting an update. Successful updates leave **RF OFF**.

In the complete FMheadend repository, see:

- [RP2354 firmware installation and update](../../docs/rp2354-firmware-update.md) — I²C uploader commands, A/B boot, rollback and recovery.
- [Controller protocol](../../docs/controller-protocol.md) — I²C command frames, status codes and SPI audio format.

These links require the full repository; the build, SWD and test instructions above work with this directory alone. Neither offline tests nor successful flash readback establish RF/audio performance or power-loss-safe OTA on every board.
