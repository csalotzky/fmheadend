#ifndef FMHEADEND_QN_CONTROL_H
#define FMHEADEND_QN_CONTROL_H
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool ready, configured, enabled, fault;
    uint8_t system1, last_error, saturated;
    int16_t attenuation;
    uint32_t failed_transactions;
} qn_channel_t;
typedef struct { qn_channel_t channel[8]; uint8_t last_error; } qn_control_t;

/* Foreground owner only. Wire statuses are the existing HOST_* values. */
void qn_control_init(qn_control_t *q, uint8_t ready_mask);
uint8_t qn_control_execute(qn_control_t *q, uint8_t command, uint8_t target,
                           const uint8_t *payload, uint8_t *result);
void qn_control_status(qn_control_t *q, uint8_t payload[130], uint32_t uptime);
bool qn_control_fault(const qn_control_t *q);
bool qn_control_active(const qn_control_t *q);
bool qn_control_led(const qn_control_t *q, uint64_t now_us);
#endif
