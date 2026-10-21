# RP2354 firmware installation and update

FMheadend uses the RP2354 boot ROM's A/B firmware mechanism with try-before-you-buy (TBYB). This guide covers initial installation over **SWD** and subsequent updates over **I²C** for the 2 MiB `rp2354-qn8x-v1` Controller.

| Method | Purpose | Requirement |
|---|---|---|
| SWD provisioning | Install the partition table and first firmware image | Debug Probe; **erases the entire flash** |
| I²C update (OTA) | Upload and activate a new image in the inactive slot | Working, previously provisioned A/B firmware |

See the [Controller protocol](controller-protocol.md#6-rp2354-firmware-update-extension) for FLASH_UPDATE message formats and the [firmware README](../firmware/fmheadend_rp2354/README.md) for standalone build and debug instructions.

## 1. Essential precautions

- **Never flash the application ELF/UF2 as a flat image at `0x10000000`.** That physical address holds the partition table. Use the provisioning script or OTA uploader.
- Select the firmware for the actual TX population and host packet size. The standard host uses **128 stereo frames per packet**.
- The supplied SWD provisioning script installs **TX8 only**; it cannot select TX1–TX7 or detect the board's physical TX population.
- Stop the backend and other Controller clients before programming. Both methods interrupt operation; neither tool stops running services for you.
- Use trusted firmware images. CRC, SHA-256 and image validation detect corruption, **not publisher authenticity**.

## 2. Flash layout and firmware images

### 2.1 Flash layout

Physical offsets, with exclusive end addresses:

| Region | Start | End | Size |
|---|---:|---:|---:|
| Partition table | `0x000000` | `0x004000` | 16 KiB |
| Application A | `0x004000` | `0x0F4000` | 960 KiB |
| Application B | `0x0F4000` | `0x1E4000` | 960 KiB |
| Unused | `0x1E4000` | `0x1FE000` | 104 KiB |
| Journal A | `0x1FE000` | `0x1FF000` | 4 KiB |
| Journal B | `0x1FF000` | `0x200000` | 4 KiB |

`partitions.json` defines the linked A/B application partitions; the journal occupies unpartitioned flash. Both images use runtime address `0x10000000`, which the ROM maps to the selected physical slot. **Do not manually rebase images or edit the partition table.**

The updater validates partition boundaries, A/B links and boot/runtime mapping before writing. Ordinary OTA erase/program operations affect the inactive slot and journal; explicit ROM confirmation can also change image metadata or invalidate the previous image.

### 2.2 Artifacts and image selection

A firmware build produces **16 images** (TX1–TX8, each in factory and OTA form), with adjacent manifests:

```text
build/factory/fmheadend_rp2354_txN.bin
build/factory/fmheadend_rp2354_txN.bin.json
build/ota/fmheadend_rp2354_txN.bin
build/ota/fmheadend_rp2354_txN.bin.json
build/partitions.bin
```

Replace `N` with 1–8. Paths above are relative to `firmware/fmheadend_rp2354/`.

| Identity / format | Current value |
|---|---|
| Application version | `1.0.0` |
| Controller protocol | 1 |
| Hardware target | `rp2354-qn8x-v1` |
| Factory ROM image | 1.0; bought (non-TBYB) |
| OTA ROM image | 1.1; TBYB |
| Build ID | 16 bytes |
| Artifact manifest / FLASH_UPDATE format | 1 / 2 |

ROM image versions are distinct from the application version; the current builds do not increase them for every OTA release. Each TX variant has a separate build identity.

**Keep each `.bin` with its matching `.bin.json`.** The validator checks SHA-256, embedded identity, image structure, size and ROM metadata. OTA accepts TBYB images, not factory images. The manifest does **not** separately identify TX count, packet size or hardware target, and the uploader does not check those properties against the board. Correct variant selection is therefore essential.

The supported image format is the build-generated, unsigned ARM Secure XIP image with a two-block `IMAGE_DEF` loop. Other image layouts, signed images and OTP anti-rollback are not supported by this workflow.

## 3. Initial installation — SWD

**Warning: provisioning erases all 2 MiB**, including both application slots and update journals. The provided script is suitable **only for TX8 hardware**.

### 3.1 Prerequisites and build

Use Windows, the Raspberry Pi Pico VS Code extension's local toolchain, an appropriate Debug Probe and the following configured tools:

| Tool | Version |
|---|---|
| Pico SDK and picotool | 2.3.1 |
| ARM GCC | 15_2_Rel1 |
| CMake / Ninja | 4.3.4 / 1.13.2 |
| OpenOCD | 0.12.0+dev |
| Python | 3.11+ |

Open `firmware/fmheadend_rp2354/` as the workspace. Build **without flashing**:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rp2354_build.ps1
```

Optional offline validation (no SWD access):

```powershell
python tools/rp2354_provision.py --build-dir build --dry-run
```

### 3.2 Provisioning

Place the Controller in **BOOTSEL** and connect the Debug Probe. This command **starts erasing/programming without confirmation**:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rp2354_build.ps1 -Provision
```

The VS Code **Flash RP2354 Factory (SWD)** task performs the same operation (`Flash Pico Project (SWD)` delegates to it).

The script:

1. Validates the TX8 factory image and partition table, then identifies the RP2350-family chip and **2 MiB flash**. It does not identify the TX population.
2. Erases the entire flash; writes `partitions.bin` at physical `0x10000000` and `factory/fmheadend_rp2354_tx8.bin` at `0x10004000`.
3. Reads back all 2 MiB and compares the table, firmware and erased areas byte-for-byte.
4. Requests a cold reset **only after successful readback**.

Logs and readback are saved as `build/provision-openocd.log` and `build/provision-readback.bin`.

If readback succeeds but reset is not acknowledged, **do not erase again just for that error**. Release BOOTSEL, reset manually and inspect Controller status. Failed readback means provisioning did not succeed. Debug configurations attach without loading; use the ELF matching the running image for symbols, not a flat flashing procedure.

## 4. Firmware update — I²C

The Linux updater is **`tools/rp2354_flash.py`**, located at the repository root. It requires the host Controller client, firmware artifact validator and installed `fmheadendd` Python package metadata; copying the script alone is insufficient.

### 4.1 Prerequisites

Use an existing correctly installed FMheadend Python environment, or create one from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install ./daemon
```

Installing the daemon package also builds a native library, so a C compiler and Python build tools are required. Keep the repository checkout available while running the uploader.

Before updating:

- **Stop the FMheadend backend and all other Controller clients.** The uploader does not stop them. Ensure exclusive access to the selected I²C device.
- Confirm that the existing bought firmware boots and the A/B layout is valid. OTA cannot install or repair a missing partition table.
- Choose the correct `.bin` and adjacent `.bin.json`, with device access permissions and a writable log location.
- Use the Controller's actual I²C device and address. The examples below use `/dev/i2c-1` and `0x62`.

### 4.2 Commands

From the repository root, with **TX8 as an example**:

```sh
# Validate the image and manifest locally; no I2C access.
python3 tools/rp2354_flash.py --dry-run firmware/fmheadend_rp2354/build/ota/fmheadend_rp2354_tx8.bin

# Read update status; accesses the Controller without starting an update.
python3 tools/rp2354_flash.py --device /dev/i2c-1 --address 0x62 --status

# Upload and activate; no interactive confirmation.
python3 tools/rp2354_flash.py --device /dev/i2c-1 --address 0x62 firmware/fmheadend_rp2354/build/ota/fmheadend_rp2354_tx8.bin
```

Use the matching TX variant for other boards. `--log PATH` changes the default `rp2354-upload.log`. `--dry-run` also creates a log; `--status` and `--dry-run` cannot be combined.

### 4.3 Update sequence

| Step | Operation |
|---|---|
| 1 | Validate image and manifest, negotiate GET_INFO and check A/B/bought-image state. |
| 2 — BEGIN | Enter maintenance, **disable RF and stop audio/DMA**, then start a fresh upload at offset zero in the inactive slot. |
| 3 — WRITE | Send up to 250 bytes per request. Firmware programs 256-byte pages and verifies flash readback; accepted bytes may temporarily remain in RAM. |
| 4 — VERIFY | Check full flash SHA-256 and image structure; record the verified image identity. |
| 5 — ACTIVATE | Request ROM trial boot of the uploaded slot after its response has been consumed. |
| 6 | Verify trial slot, build/ROM identity, TBYB state, startup-ready flag and RF OFF. |
| 7 — CONFIRM | Perform ROM explicit buy and finalization; reboot after the complete response. |
| 8 | Confirm expected slot, build/ROM and application versions, CONFIRMED state, no pending TBYB/maintenance/error and RF OFF. |

A successful update leaves **RF OFF**; restart normal host operation deliberately. The startup-ready flag does not prove RF output, audio quality or RDS reception. OTA does not modify the host SQLite database.

### 4.4 Communication failures

The uploader checks response CRCs and reconciles uncertain results with STATUS. A valid FLASH_UPDATE BUSY response is **polled, not retransmitted**.

| Operation | Recovery rule |
|---|---|
| BEGIN | Ensure the observed receiver is in the expected empty state before writing. |
| WRITE | Compare accepted/committed offsets; resend only bytes not accepted. |
| VERIFY / ACTIVATE / CONFIRM | Reconcile state and image identity; a missing response is never proof of success. |

Progress and reconciliation counts appear on the console; detailed errors are logged. Expected reboot-window ENXIO events, when confirmed by successful boot observation, are reported separately as `Reboot NACKs`. Other I²C failures remain errors.

## 5. A/B boot, confirmation and rollback

The RP2354 ROM normally boots the newer valid **bought** image; **A wins equal-version ties**. ACTIVATE explicitly boots the unbought TBYB trial image. CONFIRM buys the trial image by clearing its TBYB flag.

| Update | Why the new image boots after confirmation |
|---|---|
| Factory A 1.0 → OTA B 1.1 | B has the newer ROM version. |
| B 1.1 → OTA A 1.1 | A wins the equal-version tie. |
| A 1.1 → OTA B 1.1 | ROM buy invalidates old A so B is selected. |

During buy, the ROM **may erase the other image's first sector** if necessary to select the new one. **A/B is not an immutable factory rescue system:** later updates can overwrite A, and after confirmation both slots are not guaranteed bootable.

Before confirmation, the old bought image is the fallback if the trial fails and normal boot resumes; the ROM watchdog governs unconfirmed trials. The updater will not overwrite that fallback during a pending trial or buy a trial it cannot verify. Equal or older ROM versions are not automatically rejected, and repeating an identical build still performs an upload.

Buying changes image metadata, so the confirmed flash image no longer has exactly the uploaded file's raw SHA-256; finalization validates the expected metadata change. A cleared TBYB flag alone does not establish successful buy: the ROM result, image and journal state must agree.

These boot-selection rules are derived from the ROM implementation, **not** a hardware-qualification claim. References: [A4 boot selection](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_flash_boot.c), [A4 image launch/buy](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_launch_image.c).

## 6. Interrupted updates and recovery

| Condition | Action |
|---|---|
| Upload interrupted during reception | Restart uploader for a new BEGIN at zero; **no resume**. |
| Controller resets during upload | After bought firmware boots, start a new upload; journal may report INTERRUPTED. |
| Failed upload leaves maintenance active | RF/audio may remain off. Retry only through a permitted new BEGIN; otherwise inspect state and reset normally if appropriate. |
| Lost ACTIVATE/CONFIRM response | Let the uploader reconcile slot, build and state. Never force a buy or raw boot write. |
| Trial fails or remains unconfirmed | Allow ROM rollback/normal boot; do not write while TBYB is pending. |
| Buy succeeds but finalization/ACK is interrupted | Uploader checks existing journal and image state before starting new work. |
| Invalid layout/image, ROM failure or persistent I²C loss | Preserve logs and stop. Use a suitable SWD recovery workflow if normal recovery is unavailable. Do not erase the active slot manually. |
| SWD readback passed but reset was unconfirmed | Release BOOTSEL, reset manually and inspect status before considering another erase. |

There are **no OTA session IDs, resume or ABORT commands**. Do not interrupt active flash writes as a normal cancellation method. The two-sector CRC-sealed journal records update state, not a resumable byte-by-byte upload or host configuration.

## 7. Qualification limits

Recorded engineering runs include SWD readback, OTA activation/confirmation, reset-acknowledgment failures and I²C CRC/framing faults. They are not evidence of reliable operation on every board or bus.

**Power-loss safety has not been comprehensively qualified.** In particular, interruptions during erase/programming, journal updates, trial/confirmation and ROM buy, as well as rollback timing, still require hardware testing. ROM buy can modify both images and must not be described as an atomic, hardware-proven operation. Keep a working SWD recovery path.

## 8. Implementation references

- Firmware: `partitions.json`, `flash_update.c`, `flash_update_platform.c`, `host_protocol.c`, `host_i2c.c`, `CMakeLists.txt`, `cmake/image/CMakeLists.txt`.
- Firmware tools: `tools/rp2354_build.ps1`, `tools/rp2354_provision.py`, `tools/rp2354_artifact.py`.
- Host checkout: `tools/rp2354_flash.py`, `daemon/rp2354/protocol.py`, `daemon/rp2354/linux_client.py`, `daemon/__init__.py`, `daemon/pyproject.toml`.
