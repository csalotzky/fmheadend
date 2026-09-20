# fmheadend RP2354 firmware: M8.1

Production bring-up only: bounded QN-specific software 2-wire access, CEN-based
assignment of eight QNs to `0x21..0x28`, and RF-OFF idle boot. UART0 diagnostics
use GPIO28/29 at 115200 8N1; GPIO27 stays OFF on success and blinks on boot faults.

See [M8.1](../../docs/milestone8_1.md) for the reviewed schematic mapping, boot
sequence, build commands, exact SWD/UART procedure, tests and limitations.

- `board.h`: verified PCB GPIO mapping.
- `qn_bus.c/.h`: software 2-wire transactions and bounded error handling.
- `qn_boot.c/.h`: physical QN reset, identity, addressing and RF-OFF verification.
- `fmheadend_rp2354.c`: boot orchestration, UART reporting and diagnostic LED.
- `tests/`: native C tests with mocked GPIO, time, UART and QN register access.

The known-working `../fmheadend_i2ctest` executable remains a separate diagnostic.
M8.1 implements no Linux-facing command protocol, audio, RDS or RF-enable path.
