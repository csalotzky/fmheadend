# fmheadend RP2354 firmware: M8.6B RDS polling transport

Production firmware `0.8.6b`, protocol V1, configurable host I2C address `0x62`.
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
- `rds_fifo.c/.h`: eight independent 64-group FIFOs, atomic PUSH/CLEAR with generation tracking.
- `rds_tx.c/.h`: foreground polling handoff, acknowledgement commit and per-channel faults.
- `audio_engine.c/.h`, `i2s_audio.pio`: one PIO SM, chained DMA buffers, core 1 refill.
- `spi_audio.c/.h`, `spi_audio.pio`: PIO1 mode-0 SPI RX, one DMA channel, core1 CS IRQ.
- `spi_stream.c/.h`: exact 4100-byte validation, counter/error tracking, bounded queue.
- `audio_samples.c/.h`: 48 kHz stereo packing, independent lanes, 1 kHz -12 dBFS sine.
- `fmheadend_rp2354.c`: boot orchestration, UART and GPIO27 LED.
- `tests/`: native tests; `tools/qn_smoke.py`: real Pi bench test.

Open this directory as the Pico VS Code project; Compile Project and Flash
Project (SWD) continue to use the normal `build` target. UART remains 115200 8N1
on GPIO28/29. GPIO27 is OFF at healthy idle, ON for active TX, blinking on active
critical faults. The known-good `../fmheadend_i2ctest` is unchanged.

The shared clocks run continuously after safe boot: BCLK=1.536 MHz, WS=48 kHz,
16-bit stereo I2S. SINE=2 supplies a local tone on enabled, configured lanes;
configured DIGITAL lanes receive SPI PCM even with RF OFF (approved bench behavior).
ANALOG/unconfigured lanes and DIGITAL without fresh SPI are silent. SPI never
changes RF enable. RDS_PUSH, RDS_CLEAR and GET_RDS_FIFO_FREE provide RAM transport;
GET_STATUS reports actual FIFO capacity and consumer starvation. Eligible active
TXs transmit queued words unchanged through polling-only QN handoff. GPIO26 is
unused. See [M8.6B](../../docs/milestone8_6b.md) for state/ownership, recovery,
tests and the Pi smoke commands. Final M8.6B over-air operation needs operator validation.
See
[M8.5](../../docs/milestone8_5.md) for pins, ownership, latency, tests and exact
Pi commands; [M8.4](../../docs/milestone8_4.md) records the verified I2S timing.
See [M8.1](../../docs/milestone8_1.md)
and [M8.2](../../docs/milestone8_2.md) for historical bring-up records.
