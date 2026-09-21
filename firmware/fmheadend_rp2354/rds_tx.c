#include "rds_tx.h"
#include "qn_bus.h"
#include "qn_boot.h"
#include "host_protocol.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include <stdio.h>

enum { SYSTEM2=0x01, RDSD0=0x10, STATUS2=0x1b, RDSTXRDY=4, RXTXUPD=128 };
enum { LOG_SUBMIT=1, LOG_ACCEPT=2, LOG_FAULT=4, LOG_STARVE=8,
       LOG_RESUME=16, LOG_CLEAR=32, LOG_RECOVER=64 };
enum { FAILURE_BUS=1, FAILURE_TIMEOUT, FAILURE_CONTROL, FAILURE_RAW };

static void increment(uint32_t *n) { if (*n != UINT32_MAX) ++*n; }
static bool eligible(const qn_channel_t *c)
{ return c->ready && c->configured && c->enabled && !c->fault && (c->system1&2); }

static void fault(rds_tx_channel_t *s, qn_control_t *q, unsigned i, uint8_t reason)
{
    s->failure_stage=(uint8_t)s->phase;
    s->failure_reason=reason;
    s->phase=RDS_FAULT;
    s->events |= LOG_FAULT;
    q->channel[i].rds_fault=true;
    q->channel[i].last_error=q->last_error=HOST_QN_ERROR;
}

static void step(rds_tx_t *r, qn_control_t *q, rds_fifo_t *fifo, unsigned i)
{
    rds_tx_channel_t *s=&r->channel[i];
    qn_channel_t *c=&q->channel[i];
    uint64_t now=time_us_64();
    uint32_t lock=save_and_disable_interrupts();
    const uint32_t epoch=fifo->epoch;
    restore_interrupts(lock);

    if (epoch != s->seen_epoch) {
        s->seen_epoch=epoch;
        s->armed=s->starved=false; /* Intentional CLEAR is not starvation. */
        if (s->pending) s->events |= LOG_CLEAR;
        if (s->phase==RDS_FAULT && !s->raw_ambiguous) {
            /* CLEAR can recover a pre-handoff failure immediately. An uncertain
             * handoff must still be positively reconciled; never toggle again. */
            s->phase=s->pending ? RDS_WAIT : RDS_IDLE;
            s->deadline=now+RDS_ACK_TIMEOUT_US;
            s->next_poll=now;
            if (!s->pending) { c->rds_fault=false; s->events |= LOG_RECOVER; }
        } else if (!s->pending) s->phase=RDS_IDLE;
    }
    if (s->raw_epoch != c->rds_raw_epoch) {
        s->raw_epoch=c->rds_raw_epoch;
        if (s->pending) {
            /* Arbitrary diagnostic writes can change the handshake themselves.
             * A later toggle can no longer be attributed to our submission. */
            s->raw_ambiguous=true;
            fault(s,q,i,FAILURE_RAW);
        }
    }
    const bool active=eligible(c);
    if (s->session!=c->rds_session || active!=s->eligible) {
        s->session=c->rds_session;
        s->eligible=active;
        s->armed=s->starved=false;
        if (!s->pending && s->phase!=RDS_FAULT) s->phase=RDS_IDLE;
        /* OFF/RDS-disabled time is not a hardware acknowledgement deadline.
         * Retain the fence across stop/reconfigure; no old handoff is replayed. */
        if (active && s->pending && s->phase==RDS_WAIT) {
            s->deadline=now+RDS_ACK_TIMEOUT_US;
            s->next_poll=now;
        }
    }
    if (!active || s->phase==RDS_FAULT) return;

    uint8_t reg=0, value=0;
    bool write=false;
    switch (s->phase) {
    case RDS_IDLE: {
        lock=save_and_disable_interrupts();
        const bool have=rds_fifo_peek(fifo,&s->group);
        s->epoch=fifo->epoch;
        restore_interrupts(lock);
        if (!have) {
            if (s->armed && !s->starved) {
                increment(&c->rds_underruns);
                s->starved=true; s->events |= LOG_STARVE;
            }
            return;
        }
        if (s->starved) { s->starved=false; s->events |= LOG_RESUME; }
        reg=STATUS2; /* Baseline for this specific submission, either bit level. */
        break;
    }
    case RDS_DATA:
        reg=RDSD0+s->byte;
        value=(uint8_t)(s->group.blocks[s->byte/2] >> (s->byte%2 ? 0 : 8));
        write=true;
        break;
    case RDS_SYSTEM2: reg=SYSTEM2; break;
    case RDS_HANDOFF:
        /* Linearization point: after this short reservation, CLEAR may discard
         * the RP copy but cannot retract an already-started hardware handoff.
         * No IRQ mask covers bit-banging. CLEAR before this point cancels it. */
        lock=save_and_disable_interrupts();
        if (fifo->epoch!=s->epoch) {
            restore_interrupts(lock);
            s->phase=RDS_IDLE;
            return;
        }
        s->pending=true;
        restore_interrupts(lock);
        reg=SYSTEM2; value=s->system2^RDSTXRDY; write=true;
        s->written_system2=value;
        s->submitted_at=now;
        s->submitted_session=s->session;
        s->deadline=now+RDS_ACK_TIMEOUT_US;
        break;
    case RDS_WAIT:
        if (now<s->next_poll) return;
        reg=STATUS2;
        break;
    default: return;
    }
    const uint64_t start=time_us_64();
    const qn_bus_status_t result=write ? qn_bus_write_reg(QN_FIRST_ADDR+i,reg,value) :
                                             qn_bus_read_reg(QN_FIRST_ADDR+i,reg,&value);
    now=time_us_64();
    r->bus_us+=now-start;
    increment(&r->transactions);
    if (result!=QN_BUS_OK) {
        increment(&c->failed_transactions);
        /* No automatic replay, including a control write whose final STOP/ACK
         * failed after hardware may already have accepted it. */
        s->bus_status=(uint8_t)result;
        fault(s,q,i,FAILURE_BUS);
        return;
    }
    switch (s->phase) {
    case RDS_IDLE:
        s->baseline=s->status=value; s->byte=0; s->phase=RDS_DATA;
        break;
    case RDS_DATA:
        if (++s->byte==8) s->phase=RDS_SYSTEM2;
        break;
    case RDS_SYSTEM2:
        s->system2=value;
        if (value&0xc0) { fault(s,q,i,FAILURE_CONTROL); break; } /* Never echo reset/recalibrate. */
        s->phase=RDS_HANDOFF;
        break;
    case RDS_HANDOFF:
        increment(&s->submitted);
        s->submit_baseline=s->baseline; s->submit_system2=s->system2;
        if (s->submitted<=3) s->events |= LOG_SUBMIT;
        s->phase=RDS_WAIT; s->next_poll=now+RDS_POLL_US;
        break;
    case RDS_WAIT:
        increment(&s->polls); s->status=value;
        if ((value^s->baseline)&RXTXUPD) {
            rds_group_t discarded;
            lock=save_and_disable_interrupts();
            const bool same=fifo->epoch==s->epoch;
            if (same) (void)rds_fifo_pop(fifo,&discarded);
            restore_interrupts(lock);
            s->pending=false; s->phase=RDS_IDLE;
            /* An old post-CLEAR completion neither pops new data nor arms an
             * underrun. Likewise an interrupted lifecycle starts unarmed. */
            if (same && s->submitted_session==s->session) s->armed=true;
            s->ack_us=(uint32_t)(now-s->submitted_at);
            s->ack_baseline=s->baseline; s->ack_status=value; s->ack_system2=s->system2;
            if (s->ack_us>s->max_ack_us) s->max_ack_us=s->ack_us;
            increment(&s->accepted);
            if (s->accepted<=3) s->events |= LOG_ACCEPT;
            if (c->rds_fault) { c->rds_fault=false; s->events |= LOG_RECOVER; }
        } else if (now>=s->deadline) fault(s,q,i,FAILURE_TIMEOUT);
        else s->next_poll=now+RDS_POLL_US;
        break;
    default: break;
    }
}

void rds_tx_service(rds_tx_t *r, qn_control_t *q, rds_fifo_t fifo[8])
{
    const uint64_t start=time_us_64();
    for (unsigned n=0;n<8;++n) {
        const unsigned i=r->cursor;
        r->cursor=(i+1)%8;
        step(r,q,&fifo[i],i);
        /* At most one already-bounded bus operation can overrun the budget. */
        if (time_us_64()-start>=RDS_SERVICE_BUDGET_US) break;
    }
}

bool rds_tx_diagnostic(rds_tx_t *r, char *line, size_t size)
{
    for (unsigned n=0;n<8;++n) {
        unsigned i=r->log_cursor;
        r->log_cursor=(i+1)%8;
        rds_tx_channel_t *s=&r->channel[i];
        if (!s->events) continue;
        const unsigned event=s->events & (0u-s->events);
        s->events &= ~event;
        const char *name=event==LOG_SUBMIT ? "submitted" : event==LOG_ACCEPT ? "accepted (STATUS2 bit7 changed)" :
            event==LOG_FAULT ? "FAULT (CLEAR/reconcile or reset required)" :
            event==LOG_STARVE ? "starvation" : event==LOG_RESUME ? "resumed" :
            event==LOG_CLEAR ? "CLEAR during handoff; old acceptance fenced" : "reconciled";
        /* UART can lag the consumer. Preserve the actual acceptance pair even
         * when the next group has already replaced the live baseline. */
        const uint8_t baseline=event==LOG_ACCEPT ? s->ack_baseline :
                               event==LOG_SUBMIT ? s->submit_baseline : s->baseline;
        const uint8_t status=event==LOG_ACCEPT ? s->ack_status :
                             event==LOG_SUBMIT ? s->submit_baseline : s->status;
        const uint8_t sys2=event==LOG_ACCEPT ? s->ack_system2 :
                           event==LOG_SUBMIT ? s->submit_system2 : s->system2;
        snprintf(line,size,"RDS TX%u %s phase=%u reason=%u bus=%u baseline=%02X status=%02X SYSTEM2=%02X->%02X elapsed_us=%lu submitted=%lu accepted=%lu polls=%lu\r\n",
                 i,name,event==LOG_FAULT?s->failure_stage:(unsigned)s->phase,
                 event==LOG_FAULT?s->failure_reason:0,event==LOG_FAULT?s->bus_status:0,
                 baseline,status,sys2,(event==LOG_ACCEPT || event==LOG_SUBMIT) ? sys2^RDSTXRDY : s->written_system2,
                 (unsigned long)(event!=LOG_ACCEPT && s->pending ? time_us_64()-s->submitted_at : s->ack_us),(unsigned long)s->submitted,
                 (unsigned long)s->accepted,(unsigned long)s->polls);
        return true;
    }
    return false;
}
