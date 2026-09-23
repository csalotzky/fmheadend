# M8.1: RP2354 QN bus and safe boot

This is the first production firmware bring-up milestone in
`firmware/fmheadend_rp2354`. It is not a protocol-v1 firmware release. The Linux
backend, [protocol-v1 specification](rp2354-protocol.md) and [M6A](milestone6a.md)
are unchanged. Do not use the M6A host recovery test against this firmware yet.

## Hardware reference reviewed

The known-working `firmware/fmheadend_i2ctest/fmheadend_i2ctest.c`, its build files,
PIO source and Markdown notes were reviewed. Its executable sources/build files
are unchanged. Only the obsolete assignment warning in its
`QN8007_2wire_control_protocol.md` was corrected as requested.

The actual [PCB schematic](../firmware/fmheadend_i2ctest/fmheadend.pdf), revision
1.0 dated 2026-09-19, was inspected, especially transmitter sheet 2 and MCU sheet 4.
No conflict was found with the working reference for the M8.1 connections:

| Signal | GPIO / schematic connection |
|---|---|
| QN SDA / SCL | GPIO16 / GPIO17; R29/R30, 4.7 kohm pull-ups to 3V3_DIG |
| QN CEN0..7 | GPIO18..25; separate 10 kohm pull-downs R38..R31 to GND |
| QN SEB | Pin 21 of each U4..U11 tied to 3V3_RF (HIGH) |
| Diagnostic LED | GPIO27, MCU_STATUS, R53/D12 to GND; active HIGH |
| Debug UART0 | GPIO28 TX / GPIO29 RX; J7 pin 3 TX, pin 1 RX, pin 2 GND |
| SWD | J6 pin 1 SWCLK, pin 3 SWDIO, pin 2 GND |
| QN_INT | GPIO26, diode-combined QN interrupts; unused in M8.1 |

The CEN pull-downs support disabled QNs while RP GPIOs are inputs during reset.
At entry to `main`, firmware also actively drives every CEN LOW before UART or
bus setup, with the LOW output latch set before output enable. RF-off during the
physical power/reset transition still requires bench measurement; schematic
inspection and compiled code are not that measurement.

## Production boot behavior

1. Hold all eight CEN lines LOW. Set GPIO27 LOW. Initialize UART0 at 115200 8N1,
   no hardware flow control, on GPIO28/29. Default SDK stdio UART and USB are off.
2. Initialize the QN bus on GPIO16/17, released with pull-ups. Wait 100 ms with
   all CEN lines LOW, including on a warm RP reset.
3. For each physical index 0..7, raise only its CEN, keeping successful earlier
   devices enabled and later devices disabled. Wait the reference's 100 ms.
4. Read CID2 (`0x06`) at default address `0x2A`. Require product nibble `0x1`.
   Write the reference's RF-OFF value `SYSTEM1=0x05` at the default address.
5. Write `DEV_ADD` (`0x02`) to `0x21 + index`. Wait the reference's 10 ms, then
   read CID2 at the new address. Require the product nibble and the same CID2
   byte observed before assignment.
6. Write `SYSTEM1=0x05` at the assigned address and read it back. Require TXREQ
   (bit 6) clear. The later board crystal-load correction also writes/readbacks
   `REG_VGA=0x45` before reporting ready; see [calibration](milestone8_3.md#crystal-load-calibration).
   Only then report the device ready and leave its CEN HIGH.
7. Any bus, identity, assignment, RF-OFF or crystal-load failure immediately lowers that
   device's CEN. Continue with the other indices; addresses never compact around
   a missing device. Even an ambiguous address write cannot leave that failed
   device enabled at `0x2A` to collide with the next device.
8. Print one result per QN and an `x/8` summary. Successful idle keeps GPIO27 OFF.
   Any failed initialization leaves an unresolved unavailable-channel fault:
   GPIO27 toggles every 250 ms (2 Hz blink) after the boot pass. Repair the fault
   and reset to retry; there is no automatic recovery or runtime polling yet.

`SYSTEM1=0x05` is the RF-OFF value from the reference, not a complete digital
audio configuration. Apart from the board crystal-load setting, no SWRST, RF
enable, automatic calibration, frequency, gain or I2S configuration is performed.
Register readback confirms the requested
control state; physical absence of RF needs measurement. `qn_boot_results[8]`
and `boot_hardware_fault` are available as debug symbols through SWD.

GPIO27 remains OFF throughout boot, including while collecting failures. Steady
ON remains reserved for the later state with an active TX and no active critical
fault; address-test success never sets it ON. Initialization failures remain
active because the affected channels stay unavailable; no historical error
counter is being used as a current fault indicator.

## Bus implementation and bounds

`qn_bus.c` adapts the reference software bus with the same 5 us transition
delays. Both lines are driven LOW or released as inputs; neither is driven HIGH.
The QN read transaction is exactly:

```text
START -> address+READ -> ACK -> master sends register -> ACK
      -> QN sends data -> master NACK -> STOP
```

For example, default-address CID2 read transmits `0x55`, then `0x06`, and receives
the CID2 byte. There is no address+WRITE prefix, repeated START, SDK hardware-I2C
register read or PIO bus optimization. Only single-register operations are needed.

Every transmitted byte checks ACK. SCL release is checked with a 1 ms bound per
edge, and a 10 ms deadline covers a complete transaction. SDA must be HIGH at
idle, when transmitting a 1, during the final NACK and after STOP. On error the
driver attempts STOP, releases both lines and returns an explicit error. Cleanup
uses the same deadline (only a few fixed microsecond delays may extend it).
Read output is updated only if the complete transaction, including STOP, succeeds.

There are no unbounded retries, clock-recovery pulse trains or transaction replays.
A permanently stuck shared bus yields failed device results and a blinking LED,
not a boot hang. A healthy boot takes approximately one second; allow two seconds
for the UART summary in the bench procedure. These are expected software timings,
not measured hardware results.

## Build

From the repository root, using the already installed Windows Pico toolchain:

```powershell
$picoTools = Join-Path $env:USERPROFILE '.pico-sdk'
& "$picoTools/cmake/v4.3.4/bin/cmake.exe" `
  -S firmware/fmheadend_rp2354 -B firmware/fmheadend_rp2354/build/m81 `
  -G Ninja -DCMAKE_BUILD_TYPE=Debug `
  "-DCMAKE_MAKE_PROGRAM=$picoTools/ninja/v1.13.2/ninja.exe" `
  "-DPython3_EXECUTABLE=$env:USERPROFILE/radioconda/python.exe"
& "$picoTools/cmake/v4.3.4/bin/cmake.exe" `
  --build firmware/fmheadend_rp2354/build/m81 --parallel 4
```

The local installation supplies Pico SDK 2.3.1, Arm GNU 15.2.Rel1, CMake 4.3.4 and
Ninja 1.13.2. The existing `pico2` / `rp2350-arm-s` target used by the reference is
retained. The separate `build/m81` directory avoids the old cache's `Y:` source
path. Use an installed Python path appropriate to another machine if needed.

Outputs are `firmware/fmheadend_rp2354/build/m81/fmheadend_rp2354.elf`, `.uf2` and
`.bin`. Use this production ELF below, not the reference tone/rotation ELF.

## SWD flash and UART verification

1. With board power off, connect the Debug Probe SWD signals to J6: SWCLK pin 1,
   GND pin 2, SWDIO pin 3. Connect its UART RX to board TX (J7 pin 3 / GPIO28),
   UART TX to board RX (J7 pin 1 / GPIO29), and GND to J7 pin 2. Use 3.3 V logic
   and the board's normal power supply. Keep the RF output on the bench's
   terminated conducted measurement path.
2. Power the board and open the probe's UART COM port in HTerm (or the existing
   terminal): **115200 baud, 8 data bits, no parity, 1 stop bit, no flow control**.
   Assert **DTR**: this was necessary with the Debug Probe in the working test.
   Start text capture before programming/reset so the one-shot boot log is saved.
3. From the repository root, flash through SWD with the installed OpenOCD:

```powershell
$openocdRoot = Join-Path $env:USERPROFILE '.pico-sdk/openocd/0.12.0+dev'
$productionElf = (Resolve-Path `
  'firmware/fmheadend_rp2354/build/m81/fmheadend_rp2354.elf').Path.Replace('\', '/')
& "$openocdRoot/openocd.exe" -s "$openocdRoot/scripts" `
  -f interface/cmsis-dap.cfg -f target/rp2350.cfg `
  -c "adapter speed 5000; program {$productionElf} verify reset exit"
```

4. Require OpenOCD's programming/verification success and zero exit status.
   Its flash-byte verification is separate from firmware/hardware acceptance.
   If the initial log was missed, keep UART capture open and reset with SW1
   (MCU RUN), or execute:

```powershell
& "$openocdRoot/openocd.exe" -s "$openocdRoot/scripts" `
  -f interface/cmsis-dap.cfg -f target/rp2350.cfg `
  -c 'adapter speed 5000; init; reset run; shutdown'
```

5. Expect eight distinct successful lines, QN0 at `0x21` through QN7 at `0x28`,
   each with the actual CID2 and `RF OFF`. Typical reference CID2 is `0x11`;
   production accepts any `0x1x` revision if the before/after bytes match.

```text
fmheadend RP2354 M8.1 safe boot; RF OFF
QN0 @ 0x21: CID2=0x11 OK, RF OFF
...
QN7 @ 0x28: CID2=0x11 OK, RF OFF
BOOT: 8/8 ready; RF OFF; IDLE
```

6. GPIO27 must remain LOW/OFF during boot and indefinitely at successful idle.
   Wait at least 30 seconds: there must be no transmitter rotation or RF enable.
   Measure the conducted RF output with the bench receiver/analyzer to confirm
   no generated carrier, including during reset; UART cannot establish this.
7. Capture GPIO16/17 and CEN with a scope/logic analyzer. Verify all CENs start
   LOW, sequential enable, CID2 at `0x2A`, address writes `0x21..0x28`, CID2 at
   each assigned address and RF-OFF write/readback. Check the unusual read grammar
   manually if the analyzer assumes a conventional I2C register read. Check the
   final data byte is NACKed and both bus lines return HIGH. Successful CENs stay
   HIGH; GPIO4..13 must show no generated audio clock/data train.
8. Repeat after a cold power cycle and an RP-only reset. Both must reassign all
   devices and end RF OFF with `8/8`, rather than retaining stale addresses.

## Hardware fault tests

Use an appropriate bench fixture; never short a push-pull CEN output directly to
ground. Establish any disconnect/isolation with power off, then boot:

- Isolate one QN's CEN from the RP output and hold the QN side LOW (or use a board
  with one QN absent). Expect that index to fail, `7/8`, its MCU CEN returned LOW,
  other devices at their original index-based addresses, and a 2 Hz LED blink.
- Hold shared SDA LOW with an open-drain test fixture, then separately hold SCL
  LOW. Expect bounded failed results, `0/8` and blinking LED within two seconds.
  A held SDA should report `SDA stuck low`; held SCL should report `SCL timeout`.
  No device should be counted ready or left CEN HIGH after its failed attempt.
- For injected invalid CID2, assignment NACK/readback failure or RF-OFF readback
  failure, expect `FAIL` with the stage and either bus error or mismatch. No `OK`
  line for that device. These paths are covered by mocks; physical injection is
  optional if the bench fixture supports it safely.
- Remove the fault and reset. Require a fresh successful `8/8` boot and LED OFF.
  Removing a fault alone does not clear the unresolved boot state in M8.1.

Record board revision, firmware ELF/hash, supply, UART capture, scope captures,
RF observations, cold/warm reset results and fault-test results. Do not replace
these measurements with a compilation result.

## Automated validation and scope

Native C tests use fake Pico headers and GPIO/time/UART/QN models; they exercise
production sources without RP/QN hardware. On a machine with a native C compiler:

```sh
cmake -S firmware/fmheadend_rp2354/tests -B firmware/fmheadend_rp2354/build/tests
cmake --build firmware/fmheadend_rp2354/build/tests
ctest --test-dir firmware/fmheadend_rp2354/build/tests --output-on-failure
```

Recorded on 2026-09-20:

- Production ARM Debug build: passed; ELF, BIN and UF2 generated.
- Native C tests on the existing Linux `fmheadend` machine in an isolated `/tmp`
  directory, GCC 14.2.0, `-Wall -Wextra -Werror`: **2/2 passed**.
- Bus tests: actual GPIO transaction order, final NACK, unchanged healthy delay
  totals, NACK at every transmitted-byte stage, stuck SDA/SCL, receive-time clock
  failure, total timeout with repeated stretching, STOP failure and invalid args.
- Boot tests: **74 scenarios** including all-ready, all-missing, each of six
  operation failures at each of eight indices, invalid CID2 before/after address
  change, RF-OFF mismatch, ambiguous address-write isolation, CEN states, UART
  results and success/fault LED behavior. Invalid index is also rejected.
- **No board flashing or physical RP/QN/RF verification was performed.**

Intentionally absent: host I2C protocol, SPI audio, I2S, DMA audio, RDS, QN
interrupts, SINE generation, transmitter rotation, WebUI and Linux backend changes.
Production has no mocked hardware path; mocks exist only in native tests.

Preserved future decisions: audio remains 48 kHz stereo throughout; QN RDS status
will use polling, not QN_INT; a later `SET_TX_CONFIG.audio_mode` will add `SINE=2`.
There will be no `SET_TEST_MODE` command or production transmitter rotation.
Those future protocol changes are not made in M8.1. No architecture/protocol
deviations were introduced; the extra explicit RF-OFF writes/readback and fault
bounds are the requested production safety additions to the working reference.
