# RP2354 factory A/B firmware update

This is the initial implementation for the 2 MiB FMHeadend board, Pico SDK
2.3.1, ARM Secure XIP images. Local builds and fake-transport/state-machine
checks are available; actual board operation, timing, rollback and power-cut
behaviour have NOT been physically validated. Do not treat a successful build
as hardware qualification. No hardware was accessed during implementation.

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
From the repository root (shell line continuations may differ):

```sh
cmake -S firmware/fmheadend_rp2354 -B firmware/fmheadend_rp2354/build/factory -G Ninja -DFMHEADEND_FACTORY_IMAGE=ON -DFMHEADEND_IMAGE_MAJOR=1 -DFMHEADEND_IMAGE_MINOR=0
cmake --build firmware/fmheadend_rp2354/build/factory
cmake -S firmware/fmheadend_rp2354 -B firmware/fmheadend_rp2354/build/ota -G Ninja -DFMHEADEND_FACTORY_IMAGE=OFF -DFMHEADEND_IMAGE_MAJOR=1 -DFMHEADEND_IMAGE_MINOR=1
cmake --build firmware/fmheadend_rp2354/build/ota
picotool partition create firmware/fmheadend_rp2354/partitions.json firmware/fmheadend_rp2354/build/partitions.bin
```

Each firmware build also produces `partitions.bin` without accessing hardware.
It produces `fmheadend_rp2354.bin` and its adjacent
`fmheadend_rp2354.bin.json` manifest. The post-build validator checks the native
IMAGE_DEF, version, TBYB flag, vector addresses, block-loop structure, embedded
build identity and size. The manifest records these and the complete SHA-256.
It is generated from the actual .bin, not independently maintained metadata.
The uploader requires both files. It rejects factory (non-TBYB) images and
unsupported metadata layouts. ELF/UF2 are not uploader inputs. In particular,
do not blindly program a generated ELF at its runtime origin or a default SDK
UF2: the SDK may include an absolute compatibility block outside this 2 MiB
layout. Use the explicit .bin provisioning below.

OTA builds set `PICO_CRT0_IMAGE_TYPE_TBYB=1`; the initial factory build sets 0.
ROM versions are separate unsigned 16-bit major/minor values, not the GET_INFO
text version. Increase the ROM tuple for every update (1.0 factory, 1.1 first
update, 1.2 next, etc.). Downgrades and equal-version updates are deliberately
unsupported; this also avoids the ROM's downgrade-related other-image erase.
The embedded 16-byte build identity derives from source and build settings.

Only the emitted unsigned ARM-S native XIP two-block format is supported.
Signed images, OTP anti-rollback, extra IMAGE_DEF blocks, load maps and a changed
partition layout require a separate design; do not bypass validation. SHA-256
and CRC detect corruption, not malicious firmware: use trusted local artifacts.

## First provisioning: manual, destructive maintenance procedure

The current flat firmware without this updater/partition table cannot bootstrap
this change over I2C. The initial installation necessarily replaces that layout
and has no A/B rollback protection. Keep a known-good old image and recovery
access. Stop normal operation yourself, keep RF off, use stable power, confirm
the board really has 2 MiB, and make a flash backup with your programmer first.
None of these hardware commands was executed by the implementation agent.

USB BOOTSEL example using picotool 2.3.1, after manually entering BOOTSEL:

```sh
picotool erase -r 0x10000000 0x10200000
picotool load --ignore-partitions -v firmware/fmheadend_rp2354/build/partitions.bin -t bin -o 0x10000000
picotool reboot -u
picotool partition info
picotool load --ignore-partitions -v firmware/fmheadend_rp2354/build/factory/fmheadend_rp2354.bin -t bin -o 0x10004000
picotool reboot
```

Check the printed partition bounds before loading the factory image. B and both
journal sectors must initially be erased. The image in A is the non-TBYB 1.0
factory build; installing only a TBYB image without a previously bought image
is NOT a valid initial provisioning procedure. USB availability on the assembled
board and the programmer's actual flash-offset behaviour need manual validation.

For SWD, use an RP2350-capable programmer with both cores halted and physical
flash access: erase the 2 MiB device, write/verify partitions.bin at physical
0x000000 and the factory .bin at physical 0x004000, leave B/journal erased, then
perform a normal ROM boot. Do not flash the application ELF at 0x10000000:
that is its translated runtime address and would overwrite the table. Exact
SWD adapter configuration depends on your programmer and is not supplied as an
unverified generic command. Recovery can repeat this provisioning, or restore
your complete old-layout backup (which removes updater support).

After first boot, use CLI --status and GET_INFO to verify partition A, no pending
TBYB and successful initialization. First test A-to-B and then B-to-A with
successively higher ROM versions before relying on unattended recovery.

## Raspberry Pi CLI

Copy the repository's `tools/rp2354_flash.py`, `tools/rp2354_artifact.py`,
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

BEGIN validates ROM layout and increasing version, latches maintenance, holds
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
buys twice. Re-running the same artifact after the uploader exits also recovers
an already bought, matching ACTIVATING/CONFIRMED image; unrelated artifacts or
pending trials do not enter this recovery path. An interrupted pre-buy trial
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

Still required on your board: initial USB/SWD provisioning, actual QMI mapping
and flash readback in both slots, I2C ACK/STOP-before-reboot on the physical bus,
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
