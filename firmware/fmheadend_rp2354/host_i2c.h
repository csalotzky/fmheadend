#ifndef FMHEADEND_HOST_I2C_H
#define FMHEADEND_HOST_I2C_H
#include <stdint.h>
/* Enable only after the M8.1 boot pass. No QN access from this module. */
void host_i2c_init(uint8_t ready_mask);
/* Foreground-only UART diagnostics; I2C interrupts remain enabled. */
void host_i2c_poll(void);
#endif
