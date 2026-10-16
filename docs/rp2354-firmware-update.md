# RP2354 factory A/B firmware update

This workflow targets the 2 MiB FMHeadend RP2354 board, Pico SDK 2.3.1 and
ARM Secure XIP images. See [the workflow validation record](rp2354-workflow-validation.md)
for the precise distinction between build, simulated and hardware results.

## Physical flash layout

All intervals below are half-open physical flash offsets, not runtime addresses.

| Region | Start | End | Size |
|---|---|---|---|
| ROM partition-table reservation | 0x000000 | 0x004000 | 16 KiB |
| Application A, partition 0 | 0x004000 | 0x0F4000 | 960 KiB |
| Application B, partition 1, linked to A | 0x0F4000 | 0x1E4000 | 960 KiB |
| Unused | 0x1E4000 | 0x1FE000 | 104 KiB |
| Update journal A | 0x1FE000 | 0x1FF000 | 4 KiB |
| Update journal B | 0x1FF000 | 0x200000 | 4 KiB |

`firmware/fmheadend_rp2354/partitions.json` describes the two ROM application
partitions. The journal is reserved unpartitioned space, not a third firmware
slot. No application occupies the partition-table reservation. The firmware
requires the exact table, A/B relationship, ROM boot partition and runtime-to-
storage translation; an unknown layout fails closed before any write.

The normal SDK linker origin remains 0x10000000. The RP2350 ROM establishes QMI
address translation when launching an A/B image, mapping that runtime origin to
its physical slot. Therefore the SAME validated native .bin is used in either
slot. The updater uses storage-address ROM operations and explicitly bounds
writes to the inactive slot or the two journal sectors. It does not change
linker addresses dynamically, relocate code itself or select the boot image.

## Build and artifacts

Use Pico SDK 2.3.1, its ARM toolchain, CMake/Ninja, Python >=3.11 and picotool
2.3.1. SDK/toolchain discovery follows the project's existing CMake setup.
Open `firmware/fmheadend_rp2354` as the Pico project folder in VS Code.
**Compile / Compile Project** configures and builds both variants, without any
device access. From the repository root the identical operation is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File firmware/fmheadend_rp2354/tools/rp2354_build.ps1
```

Outputs relative to the firmware project folder:

```text
build/
  factory/fmheadend_rp2354.bin
  factory/fmheadend_rp2354.bin.json
  ota/fmheadend_rp2354.bin
  ota/fmheadend_rp2354.bin.json
  partitions.bin
```

One CMake configuration builds two executables from the same source list, with
separate SDK objects and generated headers. Factory uses
`FMHEADEND_FACTORY_IMAGE=ON`, ROM 1.0, TBYB=0; OTA uses OFF, ROM 1.1, TBYB=1.
These are target-local settings, not two independent cache configurations.
The source/build-settings fingerprint gives each variant its own build ID.
Manifest validation runs on every build, but unchanged content is not rewritten.
Missing or stale manifests are regenerated from the checked binary. Missing
binary byproducts trigger relinking. The partition binary has its own build rule.
No separate artifacts directory is used.

The validator checks IMAGE_DEF, ROM version, TBYB, vectors, block loop, embedded
identity and size; SHA-256 covers the complete binary. The uploader requires the
matching `.bin.json`. It rejects factory images and unsupported formats.
ELF/UF2 are for inspection/debug symbols, not direct provisioning at their native
0x10000000 runtime origin: doing so overwrites the partition table.

ROM versions are separate u16 major/minor values, not the GET_INFO text version.
Equal and older versions are supported by native FLASH_UPDATE boot selection.
Normally A wins a version tie. At explicit buy, the ROM erases the other image's
first sector if that image would otherwise win normal boot: for A→B this includes
equal versions; for B→A only a strictly newer old B needs erasure. Before buy the
old bought slot remains the rollback candidate. After buy this is a committed
replacement, not a guarantee of retaining both old/new images indefinitely.
See the official [A4 boot selection](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_flash_boot.c)
and [buy implementation](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_launch_image.c).
An unvalidated trial is never bought just to force a version change.

Only the emitted unsigned ARM-S native XIP two-block format is supported.
Signed images, OTP anti-rollback, extra IMAGE_DEF blocks, load maps and a changed
partition layout require a separate design; do not bypass validation. SHA-256
and CRC detect corruption, not malicious firmware: use trusted local artifacts.

## Factory provisioning: SWD, destructive

Use the extension's **Flash Pico Project (SWD)** button, or VS Code task
**Flash RP2354 Factory (SWD)**. Both run the same script. Extension 0.23.0
looks up the task named `Flash`; our task delegates to the named provisioning
task. The installed extension is not patched. Compile uses the project task
(`raspberry-pi-pico.useCmakeTools=false`) so it always builds both images and
validates their manifests; CMake/Ninja and extension SDK discovery remain intact.

The task builds first, validates the factory manifest and regenerates a temporary
partition binary for comparison, then starts SWD without interactive confirmation.
Agent-initiated hardware testing still requires BOOTSEL coordination with the
user; user-invoked Flash tasks do not. There are no automatic SWD reconnect loops.

OpenOCD identifies the RP2350-family chip and autodetects the flash; any size other
than 2 MiB fails before erase. It halts both cores, erases sectors 0–511, writes
the table at 0x10000000 and factory at 0x10004000. Python compares a complete
2 MiB readback from the uncached, untranslated 0x1c000000 alias against both input
files plus erased bytes everywhere else, including B and the journal. No target
CRC algorithm is used. Logs/readback are `build/provision-openocd.log` and
`build/provision-readback.bin`. Do not run other flashing tools concurrently.

Only after verification does the script request normal reset. If OpenOCD cannot
acknowledge that reset, the task reports failure with **flash contents verified,
normal reset unconfirmed**. Release BOOTSEL, reset manually and check `--status`;
do not erase again just because the post-write reset failed. This occurred on
the test board; the subsequent normal factory boot was verified over I²C.

Direct `Run Project` flat loading is deliberately blocked; the supplied Cortex
Debug configurations attach without loading. Inspect the active slot/build before
choosing debug symbols. Never restore a default `program ELF` Flash task for this
partitioned image. Rescue/reset tasks do not perform provisioning.

## Raspberry Pi CLI

Copy the repository's `tools/rp2354_flash.py`, `firmware/fmheadend_rp2354/tools/rp2354_artifact.py`,
`daemon/errors.py`, `daemon/__init__.py`, `daemon/rp2354/__init__.py`,
`daemon/rp2354/protocol.py` and `daemon/rp2354/linux_client.py` preserving their
relative paths, plus the .bin and .bin.json. A source checkout is simplest.
No running HTTP backend, pip dependencies or hardware-specific library is needed.
The existing Linux I2C client is reused, including CRC, two-stage reads, request
serialization and advisory flock on the device. Non-cooperating raw I2C tools
cannot be excluded by advisory locks; do not run them during maintenance.

You must stop the daemon manually before upload and retain the existing Pi
50 kHz bus configuration. The CLI never stops services or changes the bus clock.
The device-node permissions must allow the invoking user to access /dev/i2c-1.

```sh
python3 tools/rp2354_flash.py --dry-run firmware/fmheadend_rp2354/build/ota/fmheadend_rp2354.bin
python3 tools/rp2354_flash.py --device /dev/i2c-1 --address 0x62 --status
python3 tools/rp2354_flash.py --device /dev/i2c-1 --address 0x62 firmware/fmheadend_rp2354/build/ota/fmheadend_rp2354.bin
```

Upload is non-interactive. `--dry-run` validates the image/manifest locally;
`--status` is read-only. Detailed transport faults go to `rp2354-upload.log`
(or `--log PATH`); progress and cumulative error counts stay on the terminal.

## Update, maintenance and confirmation

OTA format 2 has no upload identifiers, resume or ABORT. Every BEGIN replaces
incomplete inactive-slot work and starts at offset zero, including when the
same image/version is already running. A pending TBYB trial must roll back
before a new upload; its fallback cannot be erased. A previously successful
buy with a lost ACK is finalized first, then the new upload still starts fresh.

BEGIN retains the existing RF/audio quiescence and flash-safe lockout. Failed
quiescence performs no flash writes and never restores RF automatically; a new
BEGIN retries the stop operation. Persistent hardware stop failures remain an
explicit error. Active-slot writes are rejected independently by the platform.

WRITE carries up to 250 bytes and returns a 9-byte ACK (format, accepted and
durable offsets). Packets can cross 256-byte programming pages and 4 KiB erase
sectors. Page readback precedes advancing the durable offset. VERIFY checks the
full flash SHA256 and native image metadata; build/version come from the binary.

The first response read is after 2 ms. A CRC-protected BUSY response means the
OTA reply is not ready; the same response is polled every 10 ms for at most
3 seconds, without resending the command. Partial BUSY frames remain immutable
across the V1 two READs. Other commands retain their existing timing/semantics.
CRC failures trigger bounded STATUS reconciliation; a WRITE is resent only if
its offset was not accepted. BEGIN is also reconciled against an empty, matching receiver before retrying.

ACTIVATE persists intent before its ACK and uses native ROM FLASH_UPDATE boot.
The uploader checks the actual trial slot/build/version, initialization and RF
OFF before CONFIRM. Buy, normalized full-image verification and CONFIRMED journal
writes finish before the ACK. Only the complete response and final STOP permit
the normal reboot. Lost ACKs are reconciled with STATUS and bounded replay.
Success requires the requested firmware running normally, CONFIRMED, no pending
TBYB, maintenance cleared and RF OFF. The ROM watchdog/rollback is unchanged.
Old identifier-based OTA peers are incompatible and must be replaced together;
first installation requires SWD. No bus-speed, build or provisioning change.

## Validation and remaining board qualification

OTA format 2 board run (2026-10-16): 63,612 bytes transferred in 23.5 s;
28.5 s through checked B-slot activation, CONFIRM and normal RF-OFF operation.
124 transport errors were reconciled, not ignored. The first attempt stopped
on six damaged BEGIN ACKs; adding BEGIN STATUS reconciliation allowed a fresh
run to finish. Diagnostics: `build/ota-hardware-format2.log` in the firmware
project. Initial SWD full-flash readback passed; OpenOCD's final reset failed
and required a manual normal reset. No claim of fault-free I2C is made.


Host checks cover V1 framing/CRC, split reads, update state transitions, both
slot directions/bounds, duplicate blocks, lost ACK, torn metadata, reset during
upload, hash/image rejection, stale CONFIRM, fake rollback and exclusive locking.
The review runs the Windows host C tests with Zig 0.15.2/Clang and real C11
atomic declarations, without the earlier TinyCC atomic shim. The register and
IRQ models are still deterministic/single-threaded; actual concurrent
execution requires board validation.
The portable state-machine flash/SHA callbacks are fakes, not a substitute for
physical flash or ROM tests. Firmware builds and picotool metadata inspection
check actual SDK symbols, native binary structure and version/TBYB metadata.

See [the targeted source review and regression results](rp2354-ota-review.md)
for the boot_info, partition-ID and XIP contracts, including their limits.

Remaining qualification beyond the recorded workflow tests: physical
I2C ACK/STOP timing and signal integrity on the bus,
worst-case erase/boot/confirm timing within TBYB, DMA/IRQ lockout, A/B updates,
failed/no-I2C boot, absent Pi confirmation, and power interruption at erase,
page, metadata/seal, ACTIVATE and explicit buy. Never power-cut test your only
recoverable working system. The implementation is not hardware-qualified OTA.

References: [official OTA example](https://github.com/raspberrypi/pico-examples/blob/master/pico_w/wifi/ota_update/README.md),
[RP2350 datasheet, chapter 5](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf),
installed Pico SDK 2.3.1 `pico/bootrom.h`, `pico/flash.h`, `hardware/flash.h`, and
[official ROM implementation](https://github.com/raspberrypi/pico-bootrom-rp2350).
The exact command fields and durability semantics are in the
[protocol extension](rp2354-protocol.md#extension-factory-ab-firmware-update-flash_update-0xf0).
