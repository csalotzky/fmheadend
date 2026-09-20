#ifndef FMHEADEND_HOST_I2C_H
#define FMHEADEND_HOST_I2C_H
#include <stdint.h>
#include <stdbool.h>
/* Enable only after the M8.1 boot pass. QN access only from foreground poll. */
void host_i2c_init(uint8_t ready_mask);
/* Foreground QN execution and nonblocking UART; I2C interrupts remain enabled. */
void host_i2c_poll(void);
bool host_i2c_led(uint64_t now_us);
#endif
