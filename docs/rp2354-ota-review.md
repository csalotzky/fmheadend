# RP2354 OTA targeted review — 2026-10-15

Scope: SDK/picotool 2.3.1, RP2350 A2/A3/A4 ROM, this 2 MiB CS0-only
board, unsigned ARM-S XIP images, increasing versions, no OTP rollback.
This is source/build/mock evidence. No board flash, SWD, reset or physical
qualification was performed. The existing uncommitted OTA implementation was
the review baseline; no staging, commit or push was performed.

| Question | Finding | Evidence | Action |
|---|---|---|---|
| CONFIRM / boot_info | Stale boot_info concern disproved for A2–A4; cleared pending alone is unsafe | `s_varm_api_explicit_buy`, `get_sys_info`, SDK wrapper | Keep return-code + pending + flash verification; latch failed buy against retries |
| Maintenance | Real lifecycle risk; partial stop cannot safely be resumed | `update_stop` permanently parks core1; DMA stop may fail after RF reset | Keep RF/command hold; failed-BEGIN session retained; flash-free ABORT then ACK-gated normal reboot |
| Partition IDs | Duplicate ID 0 is legal; ID is not the link index | picotool schema/parser and actual generated binary | Keep both IDs and all boundaries; offline generation test also tries B ID 1 |
| Automatic recovery | Required product improvement; reboot is appropriate | One-way audio shutdown and existing RF-OFF startup | Complete CONFIRM/ABORT ACK+STOP then normal reboot; uploader verifies final firmware and can recover lost ACK/process exit |
| Flash/XIP | SRAM wrapper is justified under this board contract | Raw ROM flash calls, SDK flash-safe/flash-start code and ARM disassembly | Preserve it; explicitly document that it is not arbitrary QMI/PSRAM state save/restore |

## Boot information and buy

The SDK [`rom_get_boot_info`](https://github.com/raspberrypi/pico-sdk/blob/2.3.1/src/rp2_common/pico_bootrom/include/pico/bootrom.h#L1122)
calls `rom_get_sys_info(SYS_INFO_BOOT_INFO)` each time; it does not cache a
snapshot in the SDK. ROM [`get_sys_info`](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_apis.c#L331)
copies the current `always.boot_type_and_diagnostics` union, which contains
`recent_boot`. [`explicit_buy`](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_launch_image.c#L147)
disables the watchdog and sets `recent_boot.tbyb_and_update_info = 0` BEFORE
the possible error exits. This also occurs in
[A2](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A2/src/main/arm/varm_launch_image.c#L122)
and [A3](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A3/src/main/arm/varm_launch_image.c#L141).

Therefore the original post-buy pending check is valid. In fact, an insufficient
buffer or other failed buy can have pending=0 while the flash flag is still
set. The ROM return code is mandatory, and the existing normalized full hash
plus IMAGE_DEF check must also succeed. The tests model flash TBYB independently
of boot_info, including failed buy with pending already cleared and failure
after a flash mutation. FAILED is latched so a subsequent CONFIRM cannot
reinterpret this error as successful buy. A synthetic success/stale-boot_info
case fails closed: it violates the checked A2–A4 contract, rather than proving
a need to replace the boot-info API with another flash-bit parser.

Buy failure normally requests immediate normal reboot at the platform layer:
the trial watchdog has been disabled by ROM. This failure path sends no
successful CONFIRM response. If reboot returns/fails, the state machine keeps
the failed-buy latch. Success uses the separate ACK-gated path only after the
CONFIRMED journal record is durable. A torn record after a successful buy is
reconstructed from ACTIVATING + actual bought image verification on next boot.

## IDs and A/B selection

The [2.3.1 schema](https://github.com/raspberrypi/picotool/blob/2.3.1/json/schemas/partition-table-schema.json)
allows an optional unsigned 64-bit `id`; there is no uniqueness constraint.
The [parser](https://github.com/raspberrypi/picotool/blob/2.3.1/main.cpp#L7896)
encodes it as `HAS_ID` plus two words. Separately, `link: ["a", 0]` writes
LINK_TYPE=A_PARTITION and LINK_VALUE=0: a reference to partition **index 0**,
not a search for a partition whose ID equals zero. The
[ROM boot scan](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_flash_boot.c#L145)
iterates resident partition indexes and picks the A/B image using link and
image metadata. The optional ID is not a boot-priority/version selector.

Changing B's ID to 1 would change identification metadata (and consumers that
search by ID, such as picotool block-device operations), not this ROM A/B boot
choice. We do not use ID-based block-device operations, and do not change IDs.
`picotool info -a partitions.bin` on the generated artifact reports:

```text
partition 0 (A):       00004000->000f4000 ... id=0000000000000000
partition 1 (B w/ 0):  000f4000->001e4000 ... id=0000000000000000
hash:                  verified
```

The offline test generates both ID variants using picotool 2.3.1 and confirms
the same link/bounds. State-machine tests run the complete upload/trial/buy/
normal-reboot cycle in both physical slot directions.

## Flash/XIP and multicore safety

SDK 2.3.1 [`rom_flash_op` and `rom_explicit_buy`](https://github.com/raspberrypi/pico-sdk/blob/2.3.1/src/rp2_common/pico_bootrom/include/pico/bootrom.h)
wrap their raw ROM call in `flash_safe_execute`; that provides CPU/IRQ exclusion,
not flash mode setup. The ROM
[`checked_flash_op`](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_checked_flash.c#L57)
validates address/permission/alignment and dispatches to raw program/erase/read.
Its erase/program path does not issue an XIP-exit sequence or restore the boot
read mode. Likewise raw explicit-buy calls the low-level erase/program routines.
Replacing our whole SRAM callback with either wrapper is not an equivalent
safety boundary for a boot-selected continuous-read mode.

Our callback looks up the declared raw entries, connects/exits/flushes flash,
calls ROM, then calls SDK
[`flash_start_xip`](https://github.com/raspberrypi/pico-sdk/blob/2.3.1/src/rp2_common/hardware_flash/flash.c).
This restores usable serial XIP and then the boot setup copied from BOOTRAM.
Neither this path nor ROM
[`flash_exit_xip`](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_generic_flash.c#L327)
calls `flash_reset_address_trans`; ATRANS survives. Normal boot sets it in
`varm_launch_image.c`. Both slot end translations are checked by `environment`.
Uncached nontranslated READ operations do not change flash modes, so the
official checked SDK read wrapper is retained.

`flash_start_xip` is an initialization operation, not an exact snapshot/restore
of every prior QMI or pad setting. In particular, our earlier exit has already
changed CS1 setup before `flash_start_xip` could save it. This is valid for the
current CS0-only board with boot-selected flash setup, no PSRAM, no custom QMI
timing/pads. Supporting such configurations would require a different reviewed
save/restore boundary. The callback must not be reused as a general flash API.

Core1 initializes the SDK lockout victim before publishing readiness. Audio
stop disables SPI CS-edge IRQ rearming, SPI/I2S PIO and DMA enables, requests
DMA abort and checks busy completion. It then parks while retaining the SDK
lockout IRQ. The platform additionally checks every DMA channel idle before
allowing mutations. Failure leaves RF held and prevents writes. The
[SDK safe zone](https://github.com/raspberrypi/pico-sdk/blob/2.3.1/src/rp2_common/pico_flash/flash.c)
parks core1 in its SRAM lockout handler and masks core0 IRQs before callback;
both are released only after XIP restoration. Read-only status/readback runs
outside mutation; no mutation is performed by the I2C IRQ. There are no enabled
application NMI handlers in this firmware. A watchdog reset or power cut remains
a hardware fault scenario, not an interrupt-masking guarantee.

The rebuilt factory and OTA disassemblies place `flash_call` at 0x20000110,
`multicore_lockout_handler` at 0x20000308, `flash_start_xip` at 0x20000428;
the XIP setup copy/call and cache-maintenance helpers are also in SRAM. The
callback's indirect entries are ROM lookups and its restoration call remains
in SRAM. This proves placement for these builds, not physical execution timing.

## Validation and provisioning decision

Host C tests: CMake/Ninja + Zig 0.15.2 (`zig cc`), all 23 CTest cases pass
(15 C executables and 8 offline Python probe/timing tests). No atomic shim is
used. Added coverage includes both slot directions, ROM buy results versus
flash/pending state, partial audio/DMA shutdown, no mutation on failed BEGIN,
ABORT recovery, torn CONFIRMED journal, duplicate/lost CONFIRM ACK, successful
response CRC/final STOP, nonempty TX FIFO, TX abort and response replacement.

Python: `pytest tools/tests daemon/tests/test_rp2354.py
daemon/tests/test_rp2354_audio.py`; set `PICOTOOL` to the 2.3.1 executable to
include the offline partition tests. The Linux device-open test is skipped on
Windows. Final result: 342 passed, 1 skipped; the only warning is the existing
Starlette/httpx deprecation. Final uploader tests check identity, ready/error/maintenance/TBYB,
finalized state, RF ON rejection, lost/partial ACKs, failure to reboot and
restarting the uploader after buy. These use fake transport/clock, not hardware.

Rebuilt artifacts: `firmware/fmheadend_rp2354/build/factory-update/` (1.0,
non-TBYB) and `firmware/fmheadend_rp2354/build/ota-review/ota/` (1.1, TBYB),
including `.bin`, manifest and `partitions.bin`. Both images are 63,172 bytes;
native image validation succeeds. The obsolete relocated `update-validation`
CMake cache is not reused. No addresses, protocol framing/READ semantics,
0x01–0x0B commands, 0xF0 fields or bus frequency were changed by this review.

No remaining source/build blocker was identified for **controlled first
provisioning on a recoverable test board**. This is not approval for unattended
production OTA. Board qualification must still demonstrate physical flash and
A/B translation in both slots, core/DMA exclusion, complete ACK before reboot
at 50 kHz, boot/buy timing within TBYB, RF-OFF startup, lost host/no-CONFIRM
rollback, and power cuts through erase/program/journal/explicit-buy. In
particular, ROM rewrites the candidate's first sector during buy; the old slot
and independent recovery access must remain available during those tests.
