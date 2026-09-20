#ifndef FMHEADEND_QN_BOOT_H
#define FMHEADEND_QN_BOOT_H

#include <stdbool.h>
#include "qn_bus.h"

#define QN_DEFAULT_ADDR 0x2Au
#define QN_FIRST_ADDR 0x21u

typedef struct {
    uint8_t address; /* Assigned only when ready; otherwise intended address. */
    uint8_t cid2;
    bool cid2_read;
    bool ready;
    const char *stage;
    qn_bus_status_t bus_status;
} qn_boot_result_t;

/* Call before diagnostics/bus activity; wait 100 ms before enabling any QN. */
void qn_boot_hold_all_reset(void);
/* Call once each, in index order 0..7. Failure lowers that QN's CEN. */
qn_boot_result_t qn_boot_device(unsigned index);

#endif
