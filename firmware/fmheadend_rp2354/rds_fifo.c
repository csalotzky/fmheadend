#include "rds_fifo.h"

_Static_assert(sizeof(rds_group_t) == RDS_GROUP_BYTES, "RDS group must be 8 bytes");

uint16_t rds_fifo_free(const rds_fifo_t *fifo)
{
    return RDS_FIFO_CAPACITY - fifo->count;
}

void rds_fifo_clear(rds_fifo_t *fifo)
{
    fifo->read_index = fifo->write_index = fifo->count = 0;
    ++fifo->epoch;
}

bool rds_fifo_push(rds_fifo_t *fifo, const uint8_t *wire, unsigned count)
{
    if (!wire || !count || count > RDS_PUSH_MAX_GROUPS ||
        count > rds_fifo_free(fifo)) return false;
    unsigned index = fifo->write_index;
    for (unsigned n = 0; n < count; ++n) {
        for (unsigned b = 0; b < 4; ++b) {
            fifo->groups[index].blocks[b] = (uint16_t)wire[0] | (uint16_t)wire[1] << 8;
            wire += 2;
        }
        index = (index + 1) % RDS_FIFO_CAPACITY;
    }
    fifo->write_index = index;
    fifo->count += count;
    return true;
}

bool rds_fifo_pop(rds_fifo_t *fifo, rds_group_t *group)
{
    if (!fifo->count) return false;
    *group = fifo->groups[fifo->read_index];
    fifo->read_index = (fifo->read_index + 1) % RDS_FIFO_CAPACITY;
    --fifo->count;
    return true;
}

bool rds_fifo_peek(const rds_fifo_t *fifo, rds_group_t *group)
{
    if (!fifo->count) return false;
    *group = fifo->groups[fifo->read_index];
    return true;
}
