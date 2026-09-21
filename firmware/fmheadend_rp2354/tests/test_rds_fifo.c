#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "rds_fifo.h"

static uint16_t value(unsigned n, unsigned block)
{
    return (uint16_t)(0x8031u + n * 313u + block * 0x1943u);
}
static void push(rds_fifo_t *fifo, unsigned first, unsigned count)
{
    uint8_t wire[31 * 8];
    for (unsigned n=0; n<count; ++n) for (unsigned b=0; b<4; ++b) {
        const uint16_t v = value(first+n,b);
        wire[n*8+b*2] = v;
        wire[n*8+b*2+1] = v >> 8;
    }
    assert(rds_fifo_push(fifo,wire,count));
}
static void pop(rds_fifo_t *fifo, unsigned expected)
{
    rds_group_t group;
    assert(rds_fifo_pop(fifo,&group));
    for (unsigned b=0; b<4; ++b) assert(group.blocks[b] == value(expected,b));
}
int main(void)
{
    rds_fifo_t fifo = {0};
    rds_group_t group = {{0xaaaa,0xffff,0x8000,0}};
    rds_group_t original = group;
    assert(sizeof(group)==8 && sizeof(fifo)==518);
    assert(rds_fifo_free(&fifo)==64 && !rds_fifo_pop(&fifo,&group));
    assert(!memcmp(&group,&original,sizeof(group))); /* Empty pop never outputs garbage. */
    uint8_t wire[31*8] = {0};
    assert(!rds_fifo_push(&fifo,wire,0));
    assert(!rds_fifo_push(&fifo,wire,32));
    assert(!rds_fifo_push(&fifo,NULL,1));
    push(&fifo,0,1); assert(rds_fifo_free(&fifo)==63); pop(&fifo,0);
    push(&fifo,1,31); push(&fifo,32,31); push(&fifo,63,2);
    assert(rds_fifo_free(&fifo)==0);
    rds_fifo_t before = fifo;
    assert(!rds_fifo_push(&fifo,wire,1) && !memcmp(&fifo,&before,sizeof(fifo)));
    for (unsigned i=1; i<=60; ++i) pop(&fifo,i);
    push(&fifo,65,31); push(&fifo,96,29); /* Multiple wrap-arounds, exact ordering. */
    for (unsigned i=61; i<=124; ++i) pop(&fifo,i);
    assert(rds_fifo_free(&fifo)==64);
    push(&fifo,200,31); push(&fifo,231,31);
    before=fifo;
    assert(!rds_fifo_push(&fifo,wire,3) && !memcmp(&fifo,&before,sizeof(fifo)));
    rds_fifo_clear(&fifo);
    assert(!fifo.read_index && !fifo.write_index && !fifo.count);
    assert(rds_fifo_free(&fifo)==64 && !rds_fifo_pop(&fifo,&group));
    push(&fifo,300,1); pop(&fifo,300); /* Cleared data cannot leak into a new queue. */
    puts("RDS FIFO: 64 groups, LE blocks, atomic batches, order, wrap, clear PASS");
}
