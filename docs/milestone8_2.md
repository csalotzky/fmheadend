# M8.2: Raspberry Pi to RP2354 I2C control skeleton

M8.2 adds the standard host control bus to the production firmware, preserving
the physically verified M8.1 QN boot procedure. It deliberately does not implement
QN configuration or RF operations. No M8.3 work, Linux backend change, WebUI,
SPI audio, I2S, sine generation, DMA audio or RDS implementation is included.
The known-good `fmheadend_i2ctest` project is unchanged.

## Pins and configurable address

The actual [PCB schematic](../firmware/fmheadend_i2ctest/fmheadend.pdf), revision
1.0 dated 2026-09-19, was inspected again on sheets 1 and 4:

| Signal | Raspberry Pi / J1 | RP2354A |
|---|---|---|
| HOST_I2C_SDA | Header pin 3, GPIO2 / SDA1 | GPIO14, package pin 18, hardware I2C1 SDA |
| HOST_I2C_SCL | Header pin 5, GPIO3 / SCL1 | GPIO15, package pin 19, hardware I2C1 SCL |
| Ground | J1 GND, e.g. pin 6 | Board common GND |

These nets are separate from QN SDA/SCL on RP GPIO16/17. R29/R30 are the **QN**
bus pull-ups; the schematic does not show separate host-bus pull-ups. The host
connection uses the Pi's SDA1/SCL1 pull-ups, with RP internal pull-ups also enabled.
Verify both host lines idle HIGH on the powered assembled setup. There is no
level translator in these schematic nets; this is a 3.3 V logic connection.
No conflict was found between the schematic and the firmware mapping.

The user-selected deployment address is **7-bit `0x62`** (wire address bytes
`0xC4` write / `0xC5` read). CMake cache setting
`FMHEADEND_HOST_I2C_ADDRESS` defaults to this explicitly selected board address
and can be changed with `-DFMHEADEND_HOST_I2C_ADDRESS=0x62`. CMake rejects values
outside `0x08..0x77` and nonnumeric input. This does not add a universal default to
protocol V1 or the Linux client. Pi `--rp-i2c-address 0x62` / persistent
`rp2354_i2c_address=98` must match the firmware build.

## Implemented commands and deliberate exceptions

The [existing V1 wire specification](rp2354-protocol.md) is unchanged. Numeric
command/status IDs, byte layouts and protocol version remain V1.

| Command | M8.2 behavior |
|---|---|
| GET_INFO | OK plus 35 payload bytes: version 1, firmware `0.8.2-stub`, hardware `PCB1.0`, eight modulators |
| GET_STATUS | OK plus a coherent 130-byte snapshot described below |
| SET_TX_CONFIG | Validate the 15-byte wire payload, decode/log all fields, return **stub OK without executing** |
| SET_TX_ENABLED | Validate one boolean byte and channel target, log ON/OFF, return **stub OK without executing** |
| RDS_PUSH / RDS_CLEAR / GET_RDS_FIFO_FREE | Validate framing/target/shape, then INVALID_COMMAND (1), empty payload |
| QN_READ_REGISTER / QN_WRITE_REGISTER | Validate framing/target/shape, then INVALID_COMMAND (1), empty payload |
| RESET_ERROR_COUNTERS / RP_SOFT_RESET | Validate global target/empty payload, then INVALID_COMMAND (1); no reset or counter changes |
| Unknown command | INVALID_COMMAND (1), empty payload |

The two SET ACKs are **intentional, user-requested temporary deviations from full
V1 command semantics**. Configuration is not applied or stored. A valid ON stub
also ACKs without a preceding configuration and even if the addressed QN failed
boot, because it does not attempt RF enable. The production configuration-before-
enable rule and hardware representability checks belong to the later real command
implementation. No fake configured/ON state is retained to hide these stubs.

SET_TX_CONFIG validation matches the existing Linux wire codec: positive u32
frequency, attenuation 0..225 in multiples of 15, boolean/enumerated fields,
preemphasis 50/75, DIGITAL=0 or ANALOG=1, and signed i16 analog gain. All i16 gain
values fit the wire representation; no board RIN or QN gain mapping is invented.
Chip tuning/gain representability is not evaluated in this milestone. SINE=2 is
still rejected; its future addition is outside M8.2.

Wrong known-command targets return INVALID_TARGET (2). Truncated/oversized frames,
wrong payload lengths and invalid field values return INVALID_ARGUMENT (3).
All error responses are exactly `[status, 0]`. Invalid writes cannot become QN
operations. RP_SOFT_RESET is never acknowledged as successful.

### Real GET_STATUS content

- Uptime is the RP's current microsecond timer divided by 1000, wrapping to u32.
- All eight `tx_state` fields are OFF (0). Successfully booted QNs had TXREQ
  cleared/read back; failed devices remain disabled with CEN LOW.
- Global state is OK after an 8/8 boot, otherwise ERROR. Global and affected
  channel error codes are QN_ERROR after a failed boot; other channels report OK.
- RF attenuation is unavailable (`-32768`), even after a config stub ACK.
- Input saturation is unavailable (`255`); no QN status polling is implemented.
- RDS FIFO free capacity is zero: no FIFO exists yet.
- SPI/audio/RDS counters are zero detected events. The corresponding transports
  and monitors are not implemented; zero is not evidence of an operational link.

This reflects the boot observations and the unchanged RF-OFF state. New QN faults
after boot are not polled yet. SET_TX_ENABLED ON leaves the snapshot OFF and never
sets GPIO27 steadily ON. M8.1 boot faults keep the LED blinking; healthy idle keeps
it OFF. Foreground UART traffic can add a short delay to the nominal 250 ms LED
toggle interval. Host validation errors do not invent QN hardware faults.

The Linux daemon can remain DEGRADED: M6A recovery reaches unsupported RDS_CLEAR,
or an expected desired-ON state disagrees with the real OFF snapshot. This is
expected. Do not change Linux recovery or fake firmware status to make it RUNNING.

## Transport and timing

The Pi writes `command:u8 target:u8 length:u8 payload[length]` in one transaction
ending in STOP. The RP serializes `status:u8 length:u8 payload[length]` at that
STOP. After at least 2 ms, the Pi reads two header bytes, ends with STOP, and then
reads exactly the payload length in a second transaction. The response cursor
survives both STOP and the master's final-byte NACK. A new completed write replaces
an abandoned response and resets the cursor. No SMBus length byte, repeated-START
register selection, CRC, new ID or sequence number is introduced.

The QN bus driver is not involved. Hardware I2C1 has a small dedicated interrupt
handler because the SDK helper combines STOP/repeated START and reports FINISH
before RECEIVE when final RX bytes and STOP are simultaneously pending. Here RX
FIFO bytes are drained before STOP commits the frame. A write followed by read
without STOP is rejected; incomplete/overlong requests never execute. Excess
bytes are drained into a bounded 258-byte request buffer and marked invalid.
Reads beyond the response return `0xFF` without overrunning memory. An address-only
probe is not a command; it does not execute or reset the response cursor.

The handler supplies one TX byte per hardware read request, so header reads do
not prefetch and lose payload bytes at STOP. Hardware FIFO-flush aborts undo the
one unsent queued response byte. Normal NACK/STOP preserves the cursor.

Only bounded byte movement, validation, snapshot serialization and log-event
queueing occur inside the IRQ. It never prints, sleeps, allocates or accesses
the QNs. Responses do not wait for the foreground/UART, including when it is busy
printing a long decoded configuration. This is the design used to meet the
documented 2 ms response deadline; physical timing still needs scope verification.

Foreground code consumes a fixed 16-event UART queue. If logging cannot keep up,
additional requests return BUSY (4) without execution or deferred replay; their
count is reported as a coalesced overload message. This bounds RAM and avoids
blocking the host on UART throughput. Request/response log pairs describe the
completed request and prepared response; they are not proof the Pi consumed it.

Host I2C becomes active only after the M8.1 boot pass and summary. Wait for the
`HOST I2C1` UART banner before testing. Early host requests may NACK during boot.

## Build and Pico VS Code workflow

Open **`firmware/fmheadend_rp2354`** as the Pico VS Code project. The generated
target remains `fmheadend_rp2354`, and the normal build directory is **`build`**.
`Compile Project` continues to run Ninja there; `Flash Project (SWD)` / the existing
SWD `Flash` task uses the production ELF selected by the extension. No custom
subdirectory or changed launch target is required. The extension settings/tasks
are preserved. The root build cache was configured and the firmware built there.

Equivalent commands from the repository root with the installed toolchain:

```powershell
$picoTools = Join-Path $env:USERPROFILE '.pico-sdk'
& "$picoTools/cmake/v4.3.4/bin/cmake.exe" `
  -S firmware/fmheadend_rp2354 -B firmware/fmheadend_rp2354/build `
  -G Ninja -DCMAKE_BUILD_TYPE=Debug -DFMHEADEND_HOST_I2C_ADDRESS=0x62 `
  "-DCMAKE_MAKE_PROGRAM=$picoTools/ninja/v1.13.2/ninja.exe" `
  "-DPython3_EXECUTABLE=$env:USERPROFILE/radioconda/python.exe"
& "$picoTools/cmake/v4.3.4/bin/cmake.exe" `
  --build firmware/fmheadend_rp2354/build --parallel 4
```

Outputs: `build/fmheadend_rp2354.elf`, `.uf2` and `.bin`. The selected address is
visible in `build/CMakeCache.txt` and the runtime UART banner. To change it,
reconfigure, rebuild, flash, and change the Pi client address to match.

SWD wiring and UART remain as documented in [M8.1](milestone8_1.md). For a manual
SWD flash from the repository root:

```powershell
$ocd = Join-Path $env:USERPROFILE '.pico-sdk/openocd/0.12.0+dev'
& "$ocd/openocd.exe" -s "$ocd/scripts" `
  -f interface/cmsis-dap.cfg -f target/rp2350.cfg `
  -c 'adapter speed 5000; program firmware/fmheadend_rp2354/build/fmheadend_rp2354.elf verify reset exit'
```

## Exact first Pi-side test

1. Connect/power the assembled Pi/headend board. Confirm J1 pins 3/5 connect to
   RP GPIO14/15 and share ground. Keep the existing terminated RF bench setup.
   Open debug UART **115200 8N1, no flow control, DTR asserted** before flashing.
2. Compile and flash the production M8.2 firmware through SWD. Require eight QN
   success lines at `0x21..0x28`, `BOOT: 8/8 ready; RF OFF; IDLE`, LED OFF, then:

   ```text
   HOST I2C1: SDA=GPIO14 SCL=GPIO15 address=0x62; V1 M8.2 STUBS; RF OFF
   ```

3. On the Pi, stop the daemon before the manual test so only one control client
   owns the bus. Confirm the Pi's standard I2C interface is enabled and that
   `/dev/i2c-1` exists. Do not run `i2cdetect`, SMBus register tools or another
   client concurrently. From a Pi checkout containing this milestone:

   ```sh
   sudo systemctl stop fmheadendd.service
   ls -l /dev/i2c-1
   # Run from the fmheadend repository root:
   sudo python3 firmware/fmheadend_rp2354/tools/host_smoke.py \
       --device /dev/i2c-1 --address 0x62
   ```

   The script uses only Python's standard library and the existing repository
   wire codec; no package installation is needed. It uses plain `os.write/read`,
   a 2 ms wait, separate header/payload reads, the existing cooperative device
   lock, 200 ms adapter timeout and zero kernel retries. Only BUSY is retried once
   after 10 ms. The response header is validated against V1 before reading the
   payload. The address is required explicitly.

4. Require GET_INFO protocol version 1, firmware `0.8.2-stub`, hardware `PCB1.0`,
   and count 8. The script refuses its ON-stub test against any other firmware
   version. It reads real status, sends a valid TX3 config and ON stub, and verifies
   the complete per-channel snapshot stays unchanged and every TX stays OFF.
   It then sends OFF and tests bad target, truncated request, invalid boolean and
   unsupported reset. Expected final line:

   ```text
   M8.2 host exchange PASS; verify UART, LED and RF separately.
   ```

5. UART should contain the following request/response pairs (plus status and
   validation-test lines); configuration values are wire units:

   ```text
   HOST RX: GET_INFO (0x01) target=0xFF length=0
   HOST TX: OK payload=35 bytes
   HOST RX: GET_STATUS (0x04) target=0xFF length=0
   HOST TX: OK payload=130 bytes
   HOST RX: SET_TX_CONFIG TX3 frequency_khz=98100 attenuation_x10=60 stereo=1 preemphasis_us=50 audio_mode=0 rds=0 gain_mode=1 analog_gain_x10=-15 digital_trim_db=1 soft_clipping=1
   HOST TX: STUB OK - NOT EXECUTED
   HOST RX: SET_TX_ENABLED TX3 ON
   HOST TX: STUB OK - NOT EXECUTED
   HOST RX: RP_SOFT_RESET (0x0A) target=0xFF length=0
   HOST TX: INVALID_COMMAND payload=0 bytes
   ```

6. LED must remain OFF on a healthy board, with no QN bus activity caused by host
   commands and no RF enable. Confirm RF remains absent using the existing bench
   measurement. With a logic analyzer on GPIO14/15, verify write STOP, then a
   two-byte read (`00 23` for INFO or `00 82` for STATUS), STOP, then the payload
   beginning at its first byte. Check header availability at the documented 2 ms
   point while UART prints a configuration. Start at the Pi's 100 kHz bus setting.
7. Repeat the smoke after an RP reset. It must redo the M8.1 address assignment,
   return a fresh INFO/status and retain RF OFF. Capture boot/UART and host-bus
   traces, ELF version/hash, board revision and physical RF/LED observations.

To try the Linux daemon afterward, its existing configuration must select the
real RP and `--i2c-device /dev/i2c-1 --rp-i2c-address 0x62`. Restart it only after
the manual client exits. DEGRADED and unsupported-command errors are expected
with these stubs; this milestone does not complete the M6A recovery sequence.

## Tests and recorded results

Native tests compile production C against fake GPIO/UART/time/I2C register models:

```sh
cmake -S firmware/fmheadend_rp2354/tests -B firmware/fmheadend_rp2354/build/tests
cmake --build firmware/fmheadend_rp2354/build/tests
ctest --test-dir firmware/fmheadend_rp2354/build/tests --output-on-failure
```

Recorded in this implementation session:

- Production ARM Debug build with Pico SDK 2.3.1 / GCC 15.2.1, address `0x62`:
  passed in the standard `build` directory; ELF/BIN/UF2 generated.
- Native C tests on the existing Linux development machine, in isolated `/tmp`,
  GCC 14.2.0 with `-Wall -Wextra -Werror`: **4/4 passed**.
- M8.1 bus tests retained unchanged. All 74 M8.1 safe-boot scenarios retained;
  only the harness time/host hooks were adapted to the foreground service loop.
- New protocol tests cover exact INFO/STATUS layout, zero padding, unavailable
  fields, boot faults, split reads, cursor replacement, over-read bounds, malformed
  and oversized writes, target/field validation, unsupported reset, deliberate
  stub ACKs with unchanged status, and bounded logging/BUSY behavior.
- New adapter tests cover pins/address, simultaneous final RX+STOP ordering,
  header/payload STOP and NACK handling, FIFO-flush cursor rollback, repeated-START
  rejection, overflow recovery and readable logs outside IRQ/critical sections.
- Final verification repeated the production build and all four native suites
  successfully. The Pi-side smoke script's `--help` check also passed with the
  actual repository wire codec and system Python, without opening an I2C device.
- **No M8.2 board flash, physical host-I2C timing test or RF test was performed
  in this implementation session.** Native mocks and compilation do not prove
  Pi/RP electrical compatibility or physical timing.

## Remaining work

Future milestones must replace the two SET stubs with actual QN configuration,
validation and safe RF lifecycle operations, then implement the remaining V1
commands and status monitoring. Full M6A recovery cannot complete until its
required commands are available. None of that work is started here.

The future 48 kHz stereo audio decision, QN RDS polling rather than QN_INT, and
later `SET_TX_CONFIG.audio_mode` SINE=2 remain preserved. No SET_TEST_MODE command
or production rotation is introduced. No commit was made for this milestone.
