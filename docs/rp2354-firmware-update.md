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
python3 tools/rp2354_flash.py --device /dev/i2c-1 --address 0x62 --yes firmware/fmheadend_rp2354/build/ota/fmheadend_rp2354.bin
```

Without --yes, upload requires typing UPDATE. --dry-run is entirely local:
artifact/manifest validation and operation plan only, no device access, session,
erase or reboot. A locked device fails immediately; the uploader never competes
with the daemon. A stale inactive session can be explicitly abandoned with
`--abort-session SESSION_HEX --yes`, followed by a new upload.

## Update, maintenance and confirmation

BEGIN validates ROM layout and image parameters, latches maintenance, holds
all transmitter reset pins low, stops SPI/I2S DMA and PIO on core1 and verifies
all DMA is idle. Core1 then remains parked with the SDK flash-lockout IRQ enabled.
Quiescence failure prevents flash writes and leaves maintenance requested.
No persistent application/RF configuration is changed. Normal RF commands and
RDS service are gated during maintenance; GET_INFO/GET_STATUS/update STATUS remain.

This is deliberately a fail-closed latch, not a transaction that restores RF.
If core1 has partially stopped, it may already be parked permanently; DMA can
still be busy and RF reset pins remain low. Releasing maintenance in place is
not safe. A failed BEGIN retains its attempted session in RAM and reports
FAILED / QUIESCE. Use `--abort-session SESSION_HEX --yes` to recover. If BEGIN
failed before quiescence completed, ABORT performs no flash writes: it reports
ABORTED in RAM and requests a normal reboot only after its complete ACK/STOP.
After that reboot the earlier durable journal (or IDLE) is visible. Once
quiescence succeeded, ABORT seals an ABORTED record before acknowledging. It
does not need to erase the candidate: an unbought TBYB image is not eligible
for normal boot. If the journal write itself fails, ABORT reports failure and
does not arm a successful-response reboot; inspect STATUS/retry after diagnosis.

Flash erase is 4 KiB aligned, programming is 256-byte aligned. Flash-safe SDK
lockout masks core0 IRQs and parks core1. One SRAM callback encloses flash mode
setup, the SDK-declared ROM entry point, and SDK XIP restoration. It never
returns to XIP code in the middle. All buffers are SRAM; DMA is stopped. The
checked flash-op lock is held around storage-address operations. The explicit-
buy call uses the same safety boundary and a word-aligned 4096-byte buffer.
Readback and SHA-256 run in foreground. There is no flash mutation in I2C IRQ.

The host waits the documented command execution window before reading. Writes
use 128-byte chunks; progress distinguishes RAM accepted from verified flash
committed bytes. Lost ACKs trigger STATUS reconciliation, not blind replay.
Interrupted RECEIVING after reset restarts safely from zero. VERIFIED and
ACTIVATING survive reset via alternating sealed CRC32 metadata records. The
previous valid record is not erased until its successor is durable.

VERIFY hashes the actual flash image and validates native image metadata.
ACTIVATE verifies again, persists intent and sends its response. Only a fully
consumed ACK plus final STOP allows a ROM FLASH_UPDATE reboot preferring the
new physical slot. No watchdog-only activation or custom boot selection exists.

The ROM provides the approximately 16.7-second TBYB watchdog. The application
does not feed/extend it or automatically buy on entry to main. The CLI uses
monotonic deadlines, GET_INFO, GET_STATUS and update STATUS to check protocol,
text/build/version, target slot, session/hash, essential initialization and
pending trial. External transmitter faults alone do not block confirmation.
It reserves time for CONFIRM and never confirms after a failed identity check.

CONFIRM validates the session/current image again before ROM explicit buy.
ROM buy clears the TBYB bit in flash; subsequent full-image verification
normalizes that bit only. If a CONFIRM ACK is lost, STATUS reconciles success.
If power fails after buy but before the CONFIRMED journal record, the matching
ACTIVATING record plus ROM state and normalized hash reconstruct confirmation.
If ROM buy returns an error, a normal ROM reboot is requested: the ROM disables
the trial watchdog even on an explicit-buy failure, so silently waiting would
be unsafe. A successful CONFIRM is not reported before verification succeeds.

Successful CONFIRM persists CONFIRMED before returning its response. Like
ACTIVATE, CONFIRM and ABORT arm reboot for the exact response generation and
subcommand. Only the complete header, payload and CRC followed by final STOP,
with an empty TX FIFO and no TX abort, releases the reboot gate. CONFIRM/ABORT
request a NORMAL ROM reboot, never another FLASH_UPDATE boot. No manual reset
is required for the successful path. Boot reinitializes audio and QNs with RF
OFF; no transmitter enable command or automatic service restart is issued.

The uploader waits for matching GET_INFO and update STATUS: correct running
slot/build/version/session/hash, no pending TBYB, CONFIRMED, ready, no update
error and maintenance=false. GET_STATUS must also report no channel ON.
External QN faults alone are not proof of a failed MCU boot. A reset gap or a
cleared BUY_PENDING flag alone is never success. For ABORT it instead verifies
the original running firmware, no pending trial and maintenance=false; the
RAM-only failed-BEGIN cancellation need not survive as a journal record.

If the full ACK/STOP happened but its result was lost on the host, polling finds
the normal boot. If the response was only partly read, matching STATUS permits
a bounded replay of CONFIRM/ABORT to obtain a new full ACK/STOP. CONFIRM never
buys twice. Explicit `--resume` with the same artifact recovers
an already bought, matching ACTIVATING/CONFIRMED image. A normal invocation
always creates a fresh session and writes the requested image, even if its build
ID and ROM version already match. `--resume` also reuses a matching RECEIVING,
VERIFIED or INTERRUPTED session; a reset-interrupted write restarts from zero.
Unrelated artifacts or pending trials do not enter the bought-image recovery path. An interrupted pre-buy trial
still relies on the ROM watchdog/rollback. A confirmed journal after reboot is
idempotent and does not cause a reboot loop. There is no unattended timeout
that reboots before a successful response has been fully read.

Without valid CONFIRM the ROM returns to the previous bought image. Missing or
corrupt session metadata cannot authorize a buy. An unbootable candidate or
broken I2C is never auto-accepted. The CLI reports the old image returning, or
an unknown final state when communication remains unavailable. Partial ACTIVATE
ACK loss can also leave the old image running; it does not prove a rollback.
The ROM's own first-sector rewrite during buy is not application-journalled;
power loss there must be tested with the old slot intact. No claim is made
that every flash silicon fault can be recovered in software.

## Validation and remaining board qualification

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
