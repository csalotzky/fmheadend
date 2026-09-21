#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "qn_rds_local_probe.h"
#include "host_protocol.h"
#include "fake_qn.h"

static qn_rds_local_probe_t probe;
static qn_control_t qn;
static bool ack_enabled, pending;
static uint64_t due, last_due;
static unsigned hardware_accepted, handoffs, bytes_written;
static uint8_t old_system2;
static unsigned data_order[8];

static void bus_step(void)
{
    if (ack_enabled && pending && fake_now>=due) {
        fake_regs[0][0x1b]^=0x80;
        pending=false;
        ++hardware_accepted;
        last_due=due;
    }
}
static void written(unsigned target, uint8_t reg, uint8_t value)
{
    assert(target==0); /* All probe writes, including RF cleanup, stay on TX0. */
    if (reg>=0x10 && reg<=0x17) {
        assert(!pending);
        assert(reg==0x10+bytes_written);
        data_order[bytes_written++]=value;
    }
    if (reg==1) {
        assert(bytes_written==8 && !pending);
        assert((value^old_system2)==4); old_system2=value;
        const uint8_t low_b[]={8,9,10,15}, low_d[]={'R','D','S',' '};
        unsigned segment=handoffs%4;
        const unsigned expected[]={0xff,0xff,0,low_b[segment],0xe0,0xcd,0x20,low_d[segment]};
        assert(!memcmp(data_order,expected,sizeof(expected)));
        bytes_written=0;
        ++handoffs;
        pending=true;
        due=last_due+87580;
        if (due<=fake_now) due=fake_now+1000;
    }
}
static void setup(void)
{
    fake_qn_reset();qn_control_init(&qn,0xff);
    memset(&probe,0,sizeof(probe));
    qn.channel[0].configured=true;qn.channel[0].enabled=true;
    qn.channel[0].system1=0x47;qn.channel[0].audio_mode=0;qn.channel[0].attenuation=225;
    fake_regs[0][0]=0x47;fake_regs[0][1]=0x1b;fake_regs[0][7]=0x71;
    fake_regs[0][8]=230;fake_regs[0][11]=0x40;fake_regs[0][15]=0x24;
    fake_regs[0][0x1b]=0x80; /* Nonzero baseline: detect both toggle directions. */
    old_system2=0x1b;
    ack_enabled=true;pending=false;due=last_due=0;
    hardware_accepted=handoffs=bytes_written=0;
    fake_operation_hook=bus_step;fake_write_hook=written;
}
static void step(void)
{
    fake_now+=1000;
    const uint64_t before=fake_now;
    qn_rds_local_probe_poll(&probe,&qn);
    assert(fake_now-before<8000); /* Healthy local service, no host waits/sleeps. */
    char line[192];
    while(qn_rds_local_probe_log(&probe,line,sizeof(line))) assert(strstr(line,"LOCAL RDS:"));
}
int main(void)
{
    setup();qn.channel[0].enabled=false;
    for(unsigned n=0;n<20;++n) step();
    assert(probe.state==QN_PROBE_ARMED && fake_calls==0); /* Safe RF-OFF idle. */
    qn.channel[0].enabled=true;
    step();assert(handoffs==1 && probe.accepted==0 && qn.channel[0].configured);
    for(unsigned n=0;n<20;++n) step();
    assert(handoffs==1 && probe.accepted==0); /* No next group before bit toggle. */
    while(probe.accepted<1) step();
    assert(handoffs==2 && probe.submitted==2); /* Next group in SAME service call. */
    while(probe.state!=QN_PROBE_DONE) step();
    assert(probe.accepted>650 && probe.accepted<700);
    assert(probe.accepted<=hardware_accepted && probe.submitted-probe.accepted<=1);
    assert(!qn.channel[0].enabled && !qn_control_fault(&qn) && !qn_control_led(&qn,0));
    assert(fake_now-probe.start_us>=60000000 && fake_now-probe.start_us<60010000);
    assert(!probe.dropped_logs);
    unsigned before=fake_calls;
    qn.channel[0].enabled=true;
    for(unsigned n=0;n<10;++n) step();
    assert(fake_calls==before); /* One-shot; RF changes cannot re-arm it. */

    setup();ack_enabled=false;step();
    while(probe.state!=QN_PROBE_DONE) step();
    assert(probe.accepted==0 && handoffs==1 && !qn.channel[0].enabled);
    assert(qn.channel[0].last_error==HOST_QN_ERROR && qn_control_fault(&qn));
    assert(fake_now-probe.handoff_us<260000);

    /* Fail each data write, the ready toggle and the initial/acceptance reads.
     * No retry/replay of a possibly accepted write. Cleanup uses existing OFF. */
    const unsigned fail_calls[]={7,8,9,10,11,12,13,14,15,16,17};
    for(unsigned i=0;i<sizeof(fail_calls)/sizeof(fail_calls[0]);++i) {
        setup();fake_fail_at=fail_calls[i];fake_fail_count=1;
        while(probe.state!=QN_PROBE_DONE) {
            fake_now+=1000;qn_rds_local_probe_poll(&probe,&qn);
            assert(fake_now<300000);
        }
        assert(!qn.channel[0].enabled && qn_control_fault(&qn) && probe.accepted==0);
        before=fake_calls;
        qn_rds_local_probe_poll(&probe,&qn);
        assert(fake_calls==before && handoffs<=1);
    }
    setup();fake_regs[0][8]=231;step();
    assert(probe.state==QN_PROBE_DONE && !handoffs && !qn.channel[0].enabled);
    setup();fake_regs[0][15]|=2;step();
    assert(probe.state==QN_PROBE_DONE && !handoffs); /* Never enables interrupt output. */
    setup();step();before=fake_calls;
    qn_rds_local_probe_cancel(&probe);
    assert(probe.state==QN_PROBE_DONE && fake_calls==before); /* Host deadline unchanged. */
    uint8_t off=0, result=0;
    assert(qn_control_execute(&qn,3,0,&off,&result)==HOST_OK && !qn.channel[0].enabled);
    setup();step();qn.channel[0].configured=false;step();
    assert(probe.state==QN_PROBE_DONE && !qn.channel[0].enabled);
    puts("Local TX0 RDS: safe arm, exact high-first groups/order, toggle pace, 60s OFF, failures, one-shot PASS");
}
