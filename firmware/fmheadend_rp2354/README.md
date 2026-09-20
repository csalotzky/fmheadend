# fmheadend RP2354 firmware: M8.3 QN control

Production firmware `0.8.3`, protocol V1, configurable host I2C address `0x62`.
The proven M8.1 software QN bus and safe boot assign eight devices to 0x21..0x28
and leave RF OFF. GET_INFO, GET_STATUS, QN_READ_REGISTER, QN_WRITE_REGISTER,
SET_TX_CONFIG and SET_TX_ENABLED execute real operations. ON requires prior
configuration, except explicitly permitted raw diagnostic writes.

See [M8.3](../../docs/milestone8_3.md) for register mappings, approved per-command
response waits, tests and exact Pi bench procedure. Use the matching Linux client;
the M8.2 2 ms client is not compatible with lengthy QN operations.

- `qn_bus.c/.h`, `qn_boot.c/.h`: unchanged verified bus and boot sequence.
- `qn_control.c/.h`: bounded foreground QN operations, state, safety and LED policy.
- `host_protocol.c/.h`: framing, validation, work handoff and response cursor.
- `host_i2c.c/.h`: hardware I2C peripheral and nonblocking foreground diagnostics.
- `fmheadend_rp2354.c`: boot orchestration, UART and GPIO27 LED.
- `tests/`: native tests; `tools/qn_smoke.py`: real Pi bench test.

Open this directory as the Pico VS Code project; Compile Project and Flash
Project (SWD) continue to use the normal `build` target. UART remains 115200 8N1
on GPIO28/29. GPIO27 is OFF at healthy idle, ON for active TX, blinking on active
critical faults. The known-good `../fmheadend_i2ctest` is unchanged.

There is no audio engine, sine generator, SPI transport or RDS FIFO/scheduler.
SINE=2 selects the QN digital input only. See [M8.1](../../docs/milestone8_1.md)
and [M8.2](../../docs/milestone8_2.md) for historical bring-up records.
