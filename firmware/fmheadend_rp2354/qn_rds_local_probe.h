#ifndef FMHEADEND_QN_RDS_LOCAL_PROBE_H
#define FMHEADEND_QN_RDS_LOCAL_PROBE_H
#include "qn_control.h"
#include <stddef.h>

/* Bench-only, core0 foreground owner. No FIFO, IRQ, allocation or GPIO26 access.
 * Zero initialization arms one run after normal host configuration/RF enable.
 */
enum qn_probe_state { QN_PROBE_ARMED, QN_PROBE_SETTLING, QN_PROBE_WAIT, QN_PROBE_DONE };
typedef struct {
    enum qn_probe_state state;
    uint8_t system2, baseline, last_status, segment;
    uint64_t start_us, stop_us, handoff_us, next_poll_us, next_report_us;
    uint64_t settle_until_us;
    uint32_t submitted, accepted, polls, max_accept_us, dropped_logs;
    char logs[8][192];
    unsigned log_head, log_tail, log_count;
} qn_rds_local_probe_t;

void qn_rds_local_probe_poll(qn_rds_local_probe_t *p, qn_control_t *q);
/* A new host mutation during a running experiment ends it before execution. */
void qn_rds_local_probe_cancel(qn_rds_local_probe_t *p);
bool qn_rds_local_probe_log(qn_rds_local_probe_t *p, char *out, size_t size);
#endif
