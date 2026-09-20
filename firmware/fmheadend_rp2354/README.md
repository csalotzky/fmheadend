# fmheadend RP2354 firmware: M8.2 control skeleton

Production bring-up only: bounded QN-specific software 2-wire access, CEN-based
assignment of eight QNs to `0x21..0x28`, and RF-OFF idle boot. UART0 diagnostics
use GPIO28/29 at 115200 8N1; GPIO27 stays OFF on success and blinks on boot faults.

See [M8.1](../../docs/milestone8_1.md) for the reviewed schematic mapping, boot
sequence, build commands, exact SWD/UART procedure, tests and limitations.
See [M8.2](../../docs/milestone8_2.md) for host I2C1 on GPIO14/15, configurable
address `0x62`, V1 framing, real boot-state reporting and temporary ACK stubs.
Firmware identifies itself as `0.8.2-stub`; RF remains OFF.

- `board.h`: verified PCB GPIO mapping.
- `qn_bus.c/.h`: software 2-wire transactions and bounded error handling.
- `qn_boot.c/.h`: physical QN reset, identity, addressing and RF-OFF verification.
- `fmheadend_rp2354.c`: boot orchestration, UART reporting and diagnostic LED.
- `host_protocol.c/.h`: bounded V1 framing, response cursor and command validation.
- `host_i2c.c/.h`: hardware I2C1 peripheral and foreground UART diagnostics.
- `tests/`: native C tests with mocked GPIO, time, UART and QN register access.
- `tools/host_smoke.py`: manual Pi test using the existing Linux wire codec.

The known-working `../fmheadend_i2ctest` executable remains a separate diagnostic.
GET_INFO/GET_STATUS are implemented. Valid SET_TX_CONFIG/SET_TX_ENABLED requests
return temporary stub OK without executing them. Other commands return errors.
There is no audio, RDS, full QN configuration or RF-enable path.
