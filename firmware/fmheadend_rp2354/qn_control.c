#include "qn_control.h"
#include "qn_bus.h"
#include "qn_boot.h"
#include "host_protocol.h"
#include "board.h"
#include "pico/stdlib.h"
#include <string.h>

/* Quintic QN8007B/8007LB rev 2.09, section 7, pp.26-43.
 * Board/reference: 26 MHz / 10 pF crystal, RIN=01 (20 kohm).
 * REG_VGA=60h retains the proven oscillator load setting, not a user default.
 */
enum { SYSTEM1=0, SYSTEM2=1, DEV_ADD=2, ANACTL1=3, REG_VGA=4,
       I2S=7, CH=8, CH_STEP=11, GAIN=13, STATUS1=26, PAG_CAL=90 };
#define TXREQ 0x40u
static void publish_sources(qn_control_t *q)
{ if (q->audio_sink) q->audio_sink(qn_control_sine_mask(q),qn_control_digital_mask(q)); }
void qn_control_set_audio_sink(qn_control_t *q, void (*sink)(uint8_t, uint8_t))
{ q->audio_sink=sink; publish_sources(q); }

static bool read_reg(qn_control_t *q, unsigned i, uint8_t reg, uint8_t *v)
{
    for (unsigned attempt=0; attempt<3; ++attempt) {
        if (qn_bus_read_reg(QN_FIRST_ADDR+i, reg, v)==QN_BUS_OK) return true;
        if (q->channel[i].failed_transactions != UINT32_MAX) ++q->channel[i].failed_transactions;
    }
    return false;
}
static bool write_reg(qn_control_t *q, unsigned i, uint8_t reg, uint8_t v)
{
    for (unsigned attempt=0; attempt<3; ++attempt) {
        if (qn_bus_write_reg(QN_FIRST_ADDR+i, reg, v)==QN_BUS_OK) return true;
        if (q->channel[i].failed_transactions != UINT32_MAX) ++q->channel[i].failed_transactions;
    }
    return false;
}
static uint8_t isolate(qn_control_t *q, unsigned i)
{
    /* Even an ambiguous write/failed read must not leave uncontrolled RF.
     * CEN LOW also prevents default-address collisions after raw SWRST.
     * Recovery requires RP reset and the proven address-assignment sequence.
     */
    gpio_put(QN_CEN_FIRST_PIN+i, 0);
    qn_channel_t *c=&q->channel[i];
    c->ready=c->configured=c->enabled=false;
    c->fault=true; c->attenuation=INT16_MIN; c->saturated=255;
    c->last_error=q->last_error=HOST_QN_ERROR;
    publish_sources(q);
    return HOST_QN_ERROR;
}
static bool checked_write(qn_control_t *q, unsigned i, uint8_t reg, uint8_t v, uint8_t mask)
{
    uint8_t actual;
    return write_reg(q,i,reg,v) && read_reg(q,i,reg,&actual) && !((actual^v)&mask);
}
void qn_control_isolate_channel(qn_control_t *q, unsigned i) { (void)isolate(q,i); }
static void observed_system1(qn_control_t *q, unsigned i, uint8_t v)
{
    qn_channel_t *c=&q->channel[i];
    if (!(v&TXREQ)) c->rds_startup_until=0;
    else if (!(c->system1&TXREQ) || (!(c->system1&2) && (v&2)))
        c->rds_startup_until=time_us_64()+QN_RDS_STARTUP_US;
    if ((c->system1^v)&~TXREQ) c->configured=false;
    if ((c->system1^v)&0x46u) ++c->rds_session;
    c->system1=v; c->enabled=(v&TXREQ)!=0;
    if (v&4) c->saturated=255;
    publish_sources(q);
}
static void observed_power(qn_channel_t *c, uint8_t v)
{
    c->attenuation=(v&0x40) ? (int16_t)((v&15)*15) : INT16_MIN;
}
void qn_control_init(qn_control_t *q, uint8_t ready_mask)
{
    memset(q,0,sizeof(*q));
    for (unsigned i=0;i<8;++i) {
        qn_channel_t *c=&q->channel[i];
        c->ready=(ready_mask&(1u<<i))!=0;
        c->system1=5; c->attenuation=INT16_MIN; c->saturated=255;
        if (!c->ready) { c->fault=true; c->last_error=q->last_error=HOST_QN_ERROR; }
    }
}
static uint8_t configure(qn_control_t *q, unsigned i, const uint8_t *p)
{
    const uint32_t freq=host_get_u32(p);
    const unsigned attenuation=host_get_u16(p+4);
    const int gain=host_get_i16(p+11);
    if (freq<76000 || freq>108000 || (freq-76000)%50 ||
        attenuation>225 || attenuation%15 || p[6]>1 || (p[7]!=50 && p[7]!=75) ||
        p[8]>2 || p[9]>1 || p[10]>1 || p[13]>1 || p[14]>1 ||
        (p[10] && (gain < -15 || gain>150 || (gain+15)%15))) return HOST_INVALID_ARGUMENT;
    /* AUTO deliberately ignores the persisted manual gains (V1 semantics).
     * ANALOG can select the chip input on all channels; PCB1.0 only routes 0..3.
     */
    qn_channel_t *c=&q->channel[i];
    if (!c->ready) return HOST_QN_ERROR;
    uint8_t old_system1, step, power, sys2;
    if (!read_reg(q,i,SYSTEM1,&old_system1)) return isolate(q,i);
    const bool restore_tx=(old_system1&TXREQ)!=0;
    ++c->rds_session;
    c->configured=false;
    publish_sources(q); /* Mute this lane before reconfiguration; clocks keep running. */
    if (!checked_write(q,i,SYSTEM1,old_system1&~TXREQ,0xff)) return isolate(q,i);
    observed_system1(q,i,old_system1&~TXREQ);
    c->attenuation=INT16_MIN;
    if (!read_reg(q,i,CH_STEP,&step) || !read_reg(q,i,PAG_CAL,&power) ||
        !read_reg(q,i,SYSTEM2,&sys2)) return isolate(q,i);
    const unsigned channel=(freq-76000)/50;
    const uint8_t system1=1u | (p[8]==1 ? 0u : 4u) | (p[9] ? 2u : 0u);
    /* Preserve RDS handshake; never SWRST. No automatic idle timeout. */
    sys2=(sys2&4u) | (p[6] ? 0u : 16u) | (p[7]==75 ? 8u : 0u) | 3u;
    const uint8_t agc=(p[14]<<7) | (p[10]<<6) |
                      (p[10] ? ((p[13]<<4) | (unsigned)((gain+15)/15)) : 1u);
    const uint8_t regs[]={SYSTEM1,ANACTL1,REG_VGA,I2S,CH,CH_STEP,GAIN,PAG_CAL};
    const uint8_t vals[]={system1,0x2b,0x60,0x71,(uint8_t)channel,
        (uint8_t)((step&0xfc)|(channel>>8)),agc,(uint8_t)((power&0xb0)|0x40|(attenuation/15))};
    /* Recalibrate while RF is OFF; preserve DEV_ADD and IPOW. */
    if (!checked_write(q,i,SYSTEM2,sys2|0x40,0xff)) return isolate(q,i);
    for (unsigned r=0;r<sizeof(regs);++r) {
        /* AUTO gain low bits are owned by the AGC FSM. */
        const uint8_t mask=regs[r]==GAIN && !p[10] ? 0xc0 : 0xff;
        if (!checked_write(q,i,regs[r],vals[r],mask)) return isolate(q,i);
    }
    if (!checked_write(q,i,SYSTEM2,sys2,0xff)) return isolate(q,i);
    sleep_ms(20); /* Proven board settling interval, not a PLL-lock measurement. */
    if (!checked_write(q,i,SYSTEM1,system1|(restore_tx?TXREQ:0),0xff)) return isolate(q,i);
    observed_system1(q,i,system1|(restore_tx?TXREQ:0));
    c->configured=true; c->attenuation=(int16_t)attenuation;
    c->audio_mode=p[8];
    publish_sources(q);
    c->saturated=255;
    return HOST_OK;
}
uint8_t qn_control_execute(qn_control_t *q, uint8_t command, uint8_t target,
                           const uint8_t *p, uint8_t *result)
{
    if (target>=8) return HOST_INVALID_TARGET;
    qn_channel_t *c=&q->channel[target];
    if (command==2) return configure(q,target,p);
    if (!c->ready) return HOST_QN_ERROR;
    uint8_t v;
    if (command==3) {
        if (p[0]>1 || (p[0] && !c->configured)) return HOST_INVALID_ARGUMENT;
        if (!read_reg(q,target,SYSTEM1,&v)) return isolate(q,target);
        v=(v&~TXREQ)|(p[0]?TXREQ:0);
        if (!checked_write(q,target,SYSTEM1,v,0xff)) return isolate(q,target);
        observed_system1(q,target,v);
    } else if (command==7) {
        if (!read_reg(q,target,p[0],&v)) return isolate(q,target);
        *result=v;
        if (p[0]==SYSTEM1) observed_system1(q,target,v);
        if (p[0]==PAG_CAL) observed_power(c,v);
    } else if (command==8) {
        ++c->rds_session;
        ++c->rds_raw_epoch;
        if (!write_reg(q,target,p[0],p[1])) return isolate(q,target);
        /* Raw diagnostics are allowed, including address/reset side effects.
         * Isolate after a route-changing write: never operate on the wrong QN.
         */
        if ((p[0]==DEV_ADD && (p[1]&0x7f)!=QN_FIRST_ADDR+target) ||
            (p[0]==SYSTEM2 && (p[1]&0x80))) { isolate(q,target); return HOST_OK; }
        if (p[0]==SYSTEM1 || p[0]==PAG_CAL) {
            if (!read_reg(q,target,p[0],&v)) return isolate(q,target);
            /* CHSC may self-clear; verify stable RF/input bits only. Manual
             * power steps must read back, while automatic calibration may move.
             */
            if (p[0]==SYSTEM1 && ((v^p[1])&0x46)) return isolate(q,target);
            if (p[0]==PAG_CAL && (p[1]&0x40) && ((v^p[1])&0x7f)) return isolate(q,target);
            if (p[0]==SYSTEM1) observed_system1(q,target,v);
            else observed_power(c,v);
        }
        /* A raw override no longer has a complete validated SET configuration. */
        if (p[0]!=SYSTEM1) c->configured=false;
        publish_sources(q);
    } else return HOST_INVALID_COMMAND;
    return HOST_OK;
}
bool qn_control_fault(const qn_control_t *q)
{ for (unsigned i=0;i<8;++i) if (q->channel[i].fault) return true; return q->audio_fault; }
void qn_control_audio_fault(qn_control_t *q, bool fault)
{
    if (fault && !q->audio_fault) q->last_error=HOST_INTERNAL_ERROR;
    q->audio_fault=fault;
}
uint8_t qn_control_sine_mask(const qn_control_t *q)
{
    uint8_t mask=0;
    for (unsigned i=0;i<8;++i) {
        const qn_channel_t *c=&q->channel[i];
        if (c->ready && c->configured && c->enabled && c->audio_mode==2 && (c->system1&4))
            mask |= 1u<<i;
    }
    return mask;
}
uint8_t qn_control_digital_mask(const qn_control_t *q)
{
    uint8_t mask=0;
    for (unsigned i=0;i<8;++i) {
        const qn_channel_t *c=&q->channel[i];
        /* Approved M8.5 bench access: configured DIGITAL PCM even with RF OFF. */
        if (c->ready && c->configured && c->audio_mode==0 && (c->system1&4)) mask |= 1u<<i;
    }
    return mask;
}
bool qn_control_active(const qn_control_t *q)
{ for (unsigned i=0;i<8;++i) if (q->channel[i].enabled) return true; return false; }
bool qn_control_led(const qn_control_t *q, uint64_t now_us)
{ return qn_control_fault(q) ? (now_us/250000)%2!=0 : qn_control_active(q); }
void qn_control_status(qn_control_t *q, uint8_t p[130], uint32_t uptime)
{
    memset(p,0,130);
    for (unsigned b=0;b<4;++b) p[1+b]=uptime>>(8*b);
    for (unsigned i=0;i<8;++i) {
        qn_channel_t *c=&q->channel[i]; uint8_t v;
        if (c->ready) {
            if (!read_reg(q,i,SYSTEM1,&v)) isolate(q,i);
            else {
                observed_system1(q,i,v);
                if (c->configured || c->attenuation!=INT16_MIN) {
                    if (!read_reg(q,i,PAG_CAL,&v)) isolate(q,i);
                    else observed_power(c,v);
                }
                if (c->ready && !(c->system1&4)) {
                    if (!read_reg(q,i,STATUS1,&v)) isolate(q,i);
                    else c->saturated=(v>>3)&1;
                }
            }
        }
        uint8_t *ch=p+10+15*i;
        ch[0]=(c->fault || c->rds_fault) ? 2 : (c->enabled ? 1 : 0);
        for (unsigned b=0;b<4;++b) ch[7+b]=c->rds_underruns>>(8*b);
        ch[1]=(uint16_t)c->attenuation; ch[2]=(uint16_t)c->attenuation>>8;
        ch[13]=c->last_error; ch[14]=c->saturated;
    }
    p[0]=qn_control_fault(q); p[9]=q->last_error;
}
