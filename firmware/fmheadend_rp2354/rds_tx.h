#ifndef FMHEADEND_RDS_TX_H
#define FMHEADEND_RDS_TX_H
#include <stddef.h>
#include "qn_control.h"
#include "rds_fifo.h"

#define RDS_ACK_TIMEOUT_US 500000u
#define RDS_POLL_US 10000u
#define RDS_SERVICE_BUDGET_US 3000u

typedef enum { RDS_IDLE, RDS_DATA, RDS_SYSTEM2, RDS_HANDOFF,
               RDS_WAIT, RDS_FAULT } rds_phase_t;
typedef struct {
    rds_phase_t phase;
    rds_group_t group;
    uint32_t epoch, seen_epoch, session, raw_epoch, submitted_session;
    uint64_t deadline, next_poll, submitted_at;
    uint32_t submitted, accepted, polls, max_ack_us, ack_us;
    uint8_t byte, baseline, status, system2, written_system2, failure_stage;
    uint8_t failure_reason, bus_status;
    uint8_t ack_baseline, ack_status, ack_system2;
    uint8_t submit_baseline, submit_system2;
    bool pending, eligible, armed, starved, raw_ambiguous;
    uint16_t events;
} rds_tx_channel_t;
typedef struct {
    rds_tx_channel_t channel[8];
    unsigned cursor, log_cursor;
    uint64_t bus_us; /* Foreground bus occupancy, inspectable through SWD. */
    uint32_t transactions;
} rds_tx_t;

/* Zero-initialized, core0 foreground only. FIFO accesses mask core0 IRQs briefly;
 * no QN transaction, UART operation or wait occurs with interrupts masked.
 * One bus operation per visit; round-robin service has a wall-time budget.
 */
void rds_tx_service(rds_tx_t *r, qn_control_t *q, rds_fifo_t fifo[8]);
bool rds_tx_diagnostic(rds_tx_t *r, char *line, size_t size);
#endif
