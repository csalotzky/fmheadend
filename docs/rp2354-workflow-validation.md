# RP2354 workflow validation — 2026-10-15

## Scope and changes

The build/provisioning workflow uses the existing Pico SDK 2.3.1, picotool 2.3.1,
CMake 4.3.4, Ninja 1.13.2 and ARM GCC 15.2 toolchain. No installed extension was
patched, no daemon configuration was changed and no RF enable command was sent.
The two-stage V1 I²C protocol and its timings are unchanged.

- `CMakeLists.txt` and `cmake/image/CMakeLists.txt`: one configure/build, two
  isolated variant targets, shared source list, always-validated manifests.
- `.vscode/tasks.json`, `settings.json`, `launch.json`: Compile builds both;
  Flash delegates to complete factory provisioning; flat Run loading is blocked;
  debug attaches without a flash load.
- `firmware/fmheadend_rp2354/tools/rp2354_build.ps1`, `firmware/fmheadend_rp2354/tools/rp2354_provision.py`: build entry point,
  mandatory BOOTSEL acknowledgement, hardware/geometry checks, full erase,
  physical-address writes and full-flash byte comparison.
- `flash_update.c`: removes the invented strictly-increasing version requirement.
  Native ROM flash-update selection and explicit buy remain authoritative.
- `tools/rp2354_flash.py`: fresh upload by default, explicit `--resume`, identity-
  checked recovery of VERIFY and activation/finalization; visible retry/error logs.
- `firmware/fmheadend_rp2354/tools/rp2354_artifact.py`: factory-aware validation and stable atomic manifest
  replacement. `linux_client.py` logs every malformed OTA response and BUSY retry.
- Targeted C/Python regression tests and `firmware/fmheadend_rp2354/AGENTS.md` preserve the workflow.

The installed Raspberry Pi Pico extension 0.23.0 `dist/extension.js` resolves SWD
Flash with `fetchTasks().find(e => "Flash" === e.name)` and executes that task.
Its Compile command bypasses the task when `useCmakeTools=true`; the project sets
it false and supplies the full configure/build task. This is a supported project
task boundary, not an extension binary modification.

## Build and simulated checks

- Both native images: 63,116 bytes; factory ROM 1.0 non-TBYB, OTA ROM 1.1 TBYB.
- Factory build ID: `3d71f19c9eb0a5ccdb24e001d8dbda5f`.
- OTA build ID: `60236f2e37d0209903913db5b105e86c`.
- Factory SHA-256: `3637854dca0ed03fa2a859aace19087e700d5801e87fbe66272f0bf1711ff716`.
- OTA SHA-256: `2bd6af389dcb1d2e8985e3e60bd72345d4e08107cb63c94ebde717173e36f45e`.
- Partition binary: 140 bytes, unchanged A/B layout/IDs.
- Clean and incremental builds produce identical bytes. Deleted manifest,
  deliberately stale manifest and deleted OTA binary were regenerated correctly.
  Unchanged source did not trigger firmware recompilation.
- Native CTest: 23/23 passed (15 C tests and 8 Python firmware tools tests).
- Broad Python run: 353 passed, 3 skipped. After the final recovery changes,
  focused uploader/provisioning run: 72 passed. Further testing was stopped at
  the user's request; no additional OTA cycle was started.
- Python regressions include same-build/same-version actual WRITE in both
  directions, lower-version requests, explicit recovery, lost VERIFY/CONFIRM
  ACKs, final identity/RF checks and full-flash readback mismatch detection.

## Real SWD result

The user acknowledged BOOTSEL separately before both attempts. First attempt
identified the device but hit a local Tcl-server startup timeout before any erase
or write; that local startup handling was corrected, with no automatic SWD retry.

Second attempt used Pico Debug Probe serial `E6616407E352312C`, OpenOCD
`0.12.0+dev-gacff23f`, SWD 1000 kHz. Observed chip ID `0x30004927`, Cortex-M33,
RP2350 rev 3, JEDEC `0x1540ef`, W25Q16JV, 2048 KiB / 512 sectors. The chip ID
identifies the RP2350 family; the board is the user-specified RP2354 with 2 MiB.

All 512 sectors erased; 140-byte partition binary written at 0x10000000 and
63,116-byte factory binary at 0x10004000. The complete 2,097,152-byte physical
readback matched both files plus 0xff everywhere else, including B and journal.
No target CRC verification was used. Readback took approximately 31.8 seconds.

**Limitation:** final OpenOCD `reset run` reported `cannot read IDR` / core1 DP
initialisation failure. The task returned failure despite verified flash contents.
After the requested manual normal reset, I²C STATUS proved factory slot A, ROM
1.0, matching build, ready=true, pending TBYB=0 and maintenance=false. Thus flash
provisioning and normal boot were verified; automatic SWD exit/reset was not.

Evidence: firmware `build/provision-openocd.log`, `build/provision-readback.bin`.

## Real OTA and communication finding

Host `192.168.1.82` identifies itself as **Raspberry Pi 3 Model B Plus Rev 1.3**,
not Pi 5. Adapter: `bcm2835 (i2c@7e804000)`, driver `i2c-bcm2835`. The daemon was
inactive; unprivileged fuser/process checks found no bus user. A root-level fuser
check was unavailable because sudo requires a password. The uploader also held
the existing advisory bus lock. No services were stopped and no bus settings
were changed.

The first A→B upload wrote all 63,116 bytes and reached durable VERIFIED, but the
VERIFY ACK had invalid CRC; the initial tool correctly exited with failure. This
exposed a missing reconciliation branch, now covered by a regression test.
After the tool fix, explicit `--resume` recovered the same session, activated B,
checked trial identity, confirmed it and observed the automatic normal reboot:
matching B/build/version, CONFIRMED, no pending TBYB, ready, no maintenance, RF off.
There was no manual reset during OTA finalization.

The subsequent B→A run uploaded the identical 63,116-byte OTA binary with the
same ROM 1.1 and build ID in a fresh session. It completed CONFIRM and observed
normal operation in slot A with RF off, without manual reset. Its log reports
246 I²C errors including reboot I/O errors; successful recovery does not qualify
the link as reliable. This is the final observed hardware state. A further
equal-version A→B cycle and arbitrary downgrades were not tested.
The B→A log remains on the Pi at `/tmp/rp2354-workflow-b-to-a-same.log`.
The final local uploader additionally factors activation recovery into an
explicit `--resume` path; that final refactor was regression-tested locally,
but was not redeployed during the already-running B→A upload.

The first upload recorded **222 malformed frames**; the recovery recorded two
more malformed frames plus eight I/O errors around reboot. Every one of these
224 CRC-failed frames had header `80 80`, XOR syndrome `0xF0B3`, and an otherwise
valid CRC if only bit 7 of the first header byte is toggled **offline for analysis**.
The uploader never repairs/accepts these corrupt frames. It rejects them and
reconciles through new CRC-checked STATUS transactions.

This narrows the observation to a first-status-byte MSB discrepancy, rather than
random payload corruption. It does **not** prove the root cause. The SDK 2.3.1
slave helper also clears RD_REQ before supplying the response byte, so a simple
reordering cannot be justified as an obvious contract fix. The BCM2835 master's
clock-stretching behavior is a relevant hypothesis: see the official kernel
[driver](https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/i2c/busses/i2c-bcm2835.c)
and [issue 254](https://github.com/raspberrypi/linux/issues/254). A logic-analyzer
capture of address ACK, stretched SCL and the first read byte, or a controlled
comparison with a different master, is still needed. Firmware IRQ service,
protocol framing, CRC validation and bus speed were not changed speculatively.

## ROM version contract and limits

The official A4 source `s_varm_crit_choose_by_tbyb_flash_update_boot_and_version`
in [varm_flash_boot.c](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_flash_boot.c)
prefers the explicitly requested valid flash-update slot regardless of ordinary
version ordering. Normal boot favors A for a tie. It records an other-image erase
address when that other image would win normal selection; explicit buy performs
the first-sector erase in `s_varm_crit_buy_erase_other_version` in
[varm_launch_image.c](https://github.com/raspberrypi/pico-bootrom-rp2350/blob/A4/src/main/arm/varm_launch_image.c).
Therefore equal/older uploads are not intrinsically forbidden. Trial rollback
is retained until buy; after commit the previous image may be invalidated by ROM.
This workflow still rejects unsupported signed/OTP anti-rollback/image layouts.

No power-cut campaign, oscilloscope/logic-analyzer timing measurement, arbitrary
downgrade matrix or complete RF/audio qualification was performed. Passing builds,
mock regressions and a recovered OTA do not make the communication error-free or
qualify unattended production OTA. The repeated CRC fault and SWD normal-reset
failure remain explicit limitations.
