#include "qn_rds_local_probe.h"
#include "qn_bus.h"
#include "qn_boot.h"
#include "host_protocol.h"
#include "pico/stdlib.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define RUN_US 60000000u
#define ACCEPT_TIMEOUT_US 250000u
#define POLL_US 2000u

/* Generated with daemon.rds.encoder.group_0a(0xffff,0,False,b" R D S  ",n).
 * Fixed BENCH content, not a production scheduler. All words high byte first.
 * PI=FFFF, PTY=0, TP=0, TA=0, music, stereo DI, no AF (E0CD).
 */
static const uint16_t groups[4][4] = {
    {0xffff,0x0008,0xe0cd,0x2052},
    {0xffff,0x0009,0xe0cd,0x2044},
    {0xffff,0x000a,0xe0cd,0x2053},
    {0xffff,0x000f,0xe0cd,0x2020},
};

static void log_line(qn_rds_local_probe_t *p, const char *format, ...)
{
    if (p->log_count==8) { ++p->dropped_logs; return; }
    va_list args;
    va_start(args,format);
    vsnprintf(p->logs[p->log_head],sizeof(p->logs[0]),format,args);
    va_end(args);
    p->log_head=(p->log_head+1)%8;
    ++p->log_count;
}
bool qn_rds_local_probe_log(qn_rds_local_probe_t *p, char *out, size_t size)
{
    if (!p->log_count || size<sizeof(p->logs[0])) return false;
    strcpy(out,p->logs[p->log_tail]);
    p->log_tail=(p->log_tail+1)%8;
    --p->log_count;
    return true;
}
static void finish(qn_rds_local_probe_t *p, qn_control_t *q, const char *why, bool fault)
{
    p->state=QN_PROBE_DONE; /* Never re-arm or repeat an uncertain handoff. */
    uint8_t off=0, unused=0;
    const uint8_t result=qn_control_execute(q,3,0,&off,&unused);
    if (fault) {
        q->channel[0].fault=true;
        q->channel[0].last_error=q->last_error=HOST_QN_ERROR;
    }
    log_line(p,"LOCAL RDS: %s; submitted=%lu accepted=%lu polls=%lu max_ack_us=%lu dropped_logs=%lu RF_OFF=%s\r\n",
        why,(unsigned long)p->submitted,(unsigned long)p->accepted,
        (unsigned long)p->polls,(unsigned long)p->max_accept_us,
        (unsigned long)p->dropped_logs,result==HOST_OK?"OK":"FAILED (QN isolated)");
}
static bool bus_result(qn_rds_local_probe_t *p, qn_control_t *q,
                       qn_bus_status_t result, uint8_t reg, const char *op)
{
    if (result==QN_BUS_OK) return true;
    log_line(p,"LOCAL RDS: %s FAIL reg=0x%02X bus_status=%u; no probe retry\r\n",op,reg,result);
    finish(p,q,"BUS FAULT",true);
    return false;
}
static bool read_reg(qn_rds_local_probe_t *p, qn_control_t *q, uint8_t reg, uint8_t *v)
{ return bus_result(p,q,qn_bus_read_reg(QN_FIRST_ADDR,reg,v),reg,"READ"); }
static bool write_reg(qn_rds_local_probe_t *p, qn_control_t *q, uint8_t reg, uint8_t v)
{ return bus_result(p,q,qn_bus_write_reg(QN_FIRST_ADDR,reg,v),reg,"WRITE"); }

static void submit(qn_rds_local_probe_t *p, qn_control_t *q)
{
    /* Snapshot before data writes, then change only RDSTXRDY. The physical
     * raw-register probe confirmed that data writes alone did not acknowledge.
     * Local QN writes do NOT invalidate the production configuration record.
     */
    if (!read_reg(p,q,0x1b,&p->baseline)) return;
    p->last_status=p->baseline;
    for (unsigned byte=0;byte<8;++byte) {
        const uint16_t word=groups[p->segment][byte/2];
        if (!write_reg(p,q,0x10+byte,(uint8_t)(word>>(byte%2?0:8)))) return;
    }
    const uint8_t before=p->system2;
    p->system2 ^= 0x04;
    p->handoff_us=time_us_64();
    if (!write_reg(p,q,0x01,p->system2)) return;
    ++p->submitted;
    p->state=QN_PROBE_WAIT;
    p->next_poll_us=time_us_64()+POLL_US;
    if (p->submitted<=4)
        log_line(p,"LOCAL RDS: TX0 submit=%lu segment=%u STATUS2=0x%02X SYSTEM2=0x%02X->0x%02X\r\n",
            (unsigned long)p->submitted,p->segment,p->baseline,before,p->system2);
}

void qn_rds_local_probe_cancel(qn_rds_local_probe_t *p)
{
    if (p->state!=QN_PROBE_WAIT) return;
    /* The following host command owns RF. Do not add another OFF transaction
     * to its approved response deadline. The probe cannot re-arm afterward. */
    p->state=QN_PROBE_DONE;
    log_line(p,"LOCAL RDS: CANCELLED by host TX0 mutation; submitted=%lu accepted=%lu; host command owns RF\r\n",
        (unsigned long)p->submitted,(unsigned long)p->accepted);
}

void qn_rds_local_probe_poll(qn_rds_local_probe_t *p, qn_control_t *q)
{
    qn_channel_t *c=&q->channel[0];
    if (p->state==QN_PROBE_DONE) return;
    if (p->state==QN_PROBE_ARMED) {
        if (!c->ready || !c->configured || !c->enabled || !(c->system1&2)) return;
        uint8_t system1, channel, step, i2s, pilot;
        if (!read_reg(p,q,0,&system1) || !read_reg(p,q,8,&channel) ||
            !read_reg(p,q,11,&step) || !read_reg(p,q,7,&i2s) ||
            !read_reg(p,q,15,&pilot) || !read_reg(p,q,1,&p->system2)) return;
        bool other_tx=false;
        for (unsigned i=1;i<8;++i) other_tx |= q->channel[i].enabled;
        if (system1!=0x47 || channel!=230 || (step&3) || i2s!=0x71 ||
            (pilot&2) || (p->system2&0xc0) || c->attenuation!=225 ||
            c->audio_mode!=0 || qn_control_fault(q) || other_tx) {
            log_line(p,"LOCAL RDS: setup rejected SYS1=%02X CH=%02X STEP=%02X I2S=%02X PILOT=%02X SYS2=%02X\r\n",
                system1,channel,step,i2s,pilot,p->system2);
            finish(p,q,"SETUP FAULT",true); return;
        }
        p->start_us=time_us_64();
        p->stop_us=p->start_us+RUN_US;
        p->next_report_us=p->start_us+10000000;
        log_line(p,"LOCAL RDS: TX0 START 60s; 87500 kHz; PI=FFFF PS=' R D S  '; high-byte-first; POLLING ONLY\r\n");
        submit(p,q); return;
    }
    const uint64_t now=time_us_64();
    if (!c->ready || !c->configured || !c->enabled || !(c->system1&2) || qn_control_fault(q)) {
        finish(p,q,"STATE CHANGED",false); return;
    }
    if (now>=p->stop_us) { finish(p,q,"60s COMPLETE",false); return; }
    if (now<p->next_poll_us) return;
    uint8_t status;
    if (!read_reg(p,q,0x1b,&status)) return;
    ++p->polls;
    const uint64_t observed=time_us_64();
    if (p->submitted<=4 && status!=p->last_status)
        log_line(p,"LOCAL RDS: TX0 STATUS2 0x%02X->0x%02X at %lu us\r\n",
            p->last_status,status,(unsigned long)(observed-p->handoff_us));
    p->last_status=status;
    if ((status^p->baseline)&0x80) {
        ++p->accepted;
        const uint32_t elapsed=(uint32_t)(observed-p->handoff_us);
        if (elapsed>p->max_accept_us) p->max_accept_us=elapsed;
        if (p->accepted<=4)
            log_line(p,"LOCAL RDS: TX0 accepted=%lu segment=%u bit7 toggled; elapsed_us=%lu\r\n",
                (unsigned long)p->accepted,p->segment,(unsigned long)elapsed);
        p->segment=(p->segment+1)%4;
        /* No host round trip, sleep, rate timer or extra group delay here. */
        if (observed<p->stop_us) submit(p,q);
        else finish(p,q,"60s COMPLETE",false);
    } else if (observed-p->handoff_us>=ACCEPT_TIMEOUT_US) {
        log_line(p,"LOCAL RDS: TIMEOUT baseline=0x%02X last=0x%02X SYSTEM2=0x%02X elapsed_us=%lu\r\n",
            p->baseline,status,p->system2,(unsigned long)(observed-p->handoff_us));
        finish(p,q,"HANDSHAKE FAULT",true);
    } else p->next_poll_us=observed+POLL_US;
    if (p->state==QN_PROBE_WAIT && observed>=p->next_report_us) {
        log_line(p,"LOCAL RDS: elapsed_ms=%lu submitted=%lu accepted=%lu polls=%lu max_ack_us=%lu\r\n",
            (unsigned long)((observed-p->start_us)/1000),(unsigned long)p->submitted,
            (unsigned long)p->accepted,(unsigned long)p->polls,(unsigned long)p->max_accept_us);
        p->next_report_us=observed+10000000;
    }
}
