#ifndef FMHEADEND_QN_BUS_H
#define FMHEADEND_QN_BUS_H

#include <stdint.h>

typedef enum {
    QN_BUS_OK,
    QN_BUS_NACK,
    QN_BUS_SCL_TIMEOUT,
    QN_BUS_SDA_STUCK,
    QN_BUS_TRANSACTION_TIMEOUT,
    QN_BUS_INVALID_ARGUMENT,
} qn_bus_status_t;

/* Single-owner synchronous QN software bus, using 7-bit addresses.
 * Each transaction is bounded, checks every ACK, attempts STOP and releases
 * both lines. No retries: an unacknowledged write may have taken effect.
 */
void qn_bus_init(void);
qn_bus_status_t qn_bus_write_reg(uint8_t address, uint8_t reg, uint8_t value);
qn_bus_status_t qn_bus_read_reg(uint8_t address, uint8_t reg, uint8_t *value);
const char *qn_bus_status_name(qn_bus_status_t status);

#endif
