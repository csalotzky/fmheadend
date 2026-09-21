#ifndef FMHEADEND_QN_CONTROL_H
#define FMHEADEND_QN_CONTROL_H
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool ready, configured, enabled, fault;
    uint8_t system1, last_error, saturated, audio_mode;
    int16_t attenuation;
    uint32_t failed_transactions;
    uint32_t rds_session, rds_raw_epoch, rds_underruns;
    bool rds_fault; /* Channel transport fault; does not disable RF/audio. */
} qn_channel_t;
typedef struct {
    qn_channel_t channel[8];
    uint8_t last_error;
    bool audio_fault;
    void (*audio_sink)(uint8_t sine, uint8_t digital); /* Optional foreground source notification. */
} qn_control_t;

/* Foreground owner only. Wire statuses are the existing HOST_* values. */
void qn_control_init(qn_control_t *q, uint8_t ready_mask);
uint8_t qn_control_execute(qn_control_t *q, uint8_t command, uint8_t target,
                           const uint8_t *payload, uint8_t *result);
void qn_control_status(qn_control_t *q, uint8_t payload[130], uint32_t uptime);
bool qn_control_fault(const qn_control_t *q);
bool qn_control_active(const qn_control_t *q);
bool qn_control_led(const qn_control_t *q, uint64_t now_us);
uint8_t qn_control_digital_mask(const qn_control_t *q);
uint8_t qn_control_sine_mask(const qn_control_t *q);
void qn_control_audio_fault(qn_control_t *q, bool fault);
void qn_control_set_audio_sink(qn_control_t *q, void (*sink)(uint8_t, uint8_t));
#endif
