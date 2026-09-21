#ifndef FMHEADEND_RDS_FIFO_H
#define FMHEADEND_RDS_FIFO_H

#include <stdbool.h>
#include <stdint.h>

#define RDS_FIFO_CAPACITY 64u
#define RDS_PUSH_MAX_GROUPS 31u
#define RDS_GROUP_BYTES 8u

typedef struct { uint16_t blocks[4]; } rds_group_t;
typedef struct {
    rds_group_t groups[RDS_FIFO_CAPACITY];
    uint16_t read_index, write_index, count;
} rds_fifo_t;

/* Zero initialization is an empty FIFO. Caller serializes ALL access.
 * Production: core0 host STOP IRQ owns push/clear/free. Foreground snapshots
 * and a future consumer must mask core0 interrupts around FIFO access only,
 * then restore interrupts BEFORE any QN operation. Core1 never accesses this
 * storage. No consumer is scheduled in M8.6A.
 */
uint16_t rds_fifo_free(const rds_fifo_t *fifo);
void rds_fifo_clear(rds_fifo_t *fifo);
/* Wire input is count * 8 bytes, four little-endian u16 blocks per group.
 * Reject invalid count or insufficient room without changing any storage.
 */
bool rds_fifo_push(rds_fifo_t *fifo, const uint8_t *wire, unsigned count);
bool rds_fifo_pop(rds_fifo_t *fifo, rds_group_t *group);

#endif
