#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "host_test_frame.h"
#include "fake_qn.h"

static host_protocol_t h;
static bool masked;
static unsigned inject_clear, inject_push;
static bool observe_reset, saw_rds_off;
static uint8_t reset_system1;
static uint8_t wire[8]={0x34,0x12,0x08,0,0xcd,0xe0,0x4d,0x46};
uint32_t save_and_disable_interrupts(void)
{ assert(!masked); masked=true; return 42; }
void restore_interrupts(uint32_t saved)
{ assert(masked && saved==42); masked=false; }
static void bus_hook(void)
{
    assert(!masked);
    if (observe_reset) {
        assert((fake_regs[0][0]&~2u)==(reset_system1&~2u));
        assert(h.qn.channel[0].configured && h.qn.channel[0].enabled);
        assert(qn_control_digital_mask(&h.qn)&1);
        if (!(fake_regs[0][0]&2)) saw_rds_off=true;
    }
    if (inject_clear) {
        inject_clear=0; rds_fifo_clear(&h.rds[0]);
        if (inject_push) { assert(rds_fifo_push(&h.rds[0],wire,1)); inject_push=0; }
    }
}
static void reset(void)
{
    fake_qn_reset(); host_protocol_init(&h,255);
    masked=false; inject_clear=inject_push=0; fake_operation_hook=bus_hook;
    observe_reset=saw_rds_off=false;
}
static void config(unsigned i, bool rds)
{
    uint8_t p[15]={0xcc,0x55,1,0,225,0,1,50,0,0,0,0,0,0,0}, out;
    p[9]=rds;
    assert(qn_control_execute(&h.qn,2,i,p,&out)==HOST_OK);
    if(fake_now<h.qn.channel[i].rds_startup_until) fake_now=h.qn.channel[i].rds_startup_until;
}
static void enable(unsigned i, bool on)
{
    uint8_t v=on,out;
    assert(qn_control_execute(&h.qn,3,i,&v,&out)==HOST_OK);
    if(fake_now<h.qn.channel[i].rds_startup_until) fake_now=h.qn.channel[i].rds_startup_until;
}
static void active(unsigned i) { config(i,true); enable(i,true); }
static void tick(void)
{ rds_tx_service(&h.rds_tx,&h.qn,h.rds); fake_now+=1000; }
static void ticks(unsigned n) { for (unsigned i=0;i<n;++i) tick(); }
static void push(unsigned i) { assert(rds_fifo_push(&h.rds[i],wire,1)); }
static void until(unsigned i,rds_phase_t phase)
{
    for (unsigned n=0;n<100 && h.rds_tx.channel[i].phase!=phase;++n) tick();
    assert(h.rds_tx.channel[i].phase==phase);
}
static void acknowledge(unsigned i)
{
    fake_regs[i][0x1b]^=0x80;
    fake_now+=RDS_POLL_US;
    tick();
}
static void first(void) { reset();active(0);push(0);until(0,RDS_WAIT); }
static void host_command(uint8_t cmd,uint8_t target)
{
    uint8_t req[]={cmd,target,0};
    test_receive(&h,req,sizeof(req));
    host_protocol_stop(&h,0);
    host_work_t w;
    if(host_protocol_take_work(&h,&w)) {
        host_protocol_run_work(&h,&w,42); host_protocol_complete_work(&h,&w);
    }
    assert(h.response[0]==HOST_OK);
    host_log_event_t e; while(host_protocol_pop_log(&h,&e)) {}
}
static void exact_order(void)
{
    reset();active(0);
    fake_regs[0][1]=0x3b; /* Stereo/preemphasis/timeout bits survive XOR. */
    fake_regs[0][0x1b]=0x83; /* First transition is HIGH -> LOW. */
    uint8_t batch[24];
    for(unsigned i=0;i<24;++i) batch[i]=(uint8_t)(i*73+19);
    assert(rds_fifo_push(&h.rds[0],batch,3));
    for(unsigned group=0;group<3;++group) {
        until(0,RDS_WAIT);
        assert(h.rds[0].count==3-group);
        for(unsigned b=0;b<8;++b) assert(fake_regs[0][0x10+b]==batch[group*8+(b^1)]);
        assert(fake_regs[0][1]==(uint8_t)(0x3b ^ ((group%2==0)?4:0)));
        unsigned writes=fake_writes;
        fake_regs[0][0x1b]^=1; /* Other status bits never count as acceptance. */
        ticks(30);
        assert(h.rds[0].count==3-group && fake_writes==writes);
        acknowledge(0);
        assert(h.rds[0].count==2-group);
        assert(h.rds_tx.channel[0].accepted==group+1);
    }
    ticks(50);
    assert(h.rds[0].count==0 && h.rds_tx.channel[0].accepted==3);
    assert(h.qn.channel[0].rds_underruns==1);
    host_command(11,255);assert(h.response[1]==16 && host_get_u16(h.response+2)==64);
    host_command(4,255);assert(h.response[1]==130);
    assert(host_get_u32(h.response+2+10+7)==1 && host_get_u16(h.response+2+10+11)==64);
    assert(h.response[2+10]==1 && !h.response[2]);
    assert(h.qn.channel[0].enabled && qn_control_led(&h.qn,0));
}
static void starvation_and_lifecycle(void)
{
    reset();ticks(10);config(0,true);ticks(10);enable(0,true);ticks(50);
    assert(h.qn.channel[0].rds_underruns==0); /* Startup empty is not starvation. */
    push(0);until(0,RDS_WAIT);acknowledge(0);ticks(100);
    assert(h.qn.channel[0].rds_underruns==1);
    push(0);until(0,RDS_WAIT);acknowledge(0);ticks(100);
    assert(h.qn.channel[0].rds_underruns==2);
    host_command(6,0);ticks(100);
    assert(h.qn.channel[0].rds_underruns==2);
    enable(0,false);push(0);unsigned writes=fake_writes;ticks(100);
    assert(fake_writes==writes && h.rds[0].count==1);
    host_command(6,0); /* Linux stop/start boundary discards old content. */
    enable(0,true);ticks(30);assert(h.qn.channel[0].rds_underruns==2);
    push(0);until(0,RDS_WAIT);
    enable(0,false);host_command(6,0);push(0);
    fake_now+=2*RDS_ACK_TIMEOUT_US;ticks(5);
    assert(!h.qn.channel[0].rds_fault && h.rds_tx.channel[0].pending);
    enable(0,true);tick();acknowledge(0); /* Old acknowledgement cannot pop new FIFO. */
    assert(h.rds[0].count==1);
    until(0,RDS_WAIT);acknowledge(0);assert(h.rds[0].count==0);
    /* Disable RDS during a handoff; preserve RF/audio, no polling while disabled. */
    push(0);until(0,RDS_WAIT);config(0,false);host_command(6,0);
    assert(h.qn.channel[0].enabled);
    unsigned calls=fake_calls;fake_now+=2*RDS_ACK_TIMEOUT_US;ticks(20);
    assert(fake_calls==calls && !h.qn.channel[0].rds_fault);
    config(0,true);push(0);tick();acknowledge(0);
    assert(h.rds[0].count==1);until(0,RDS_WAIT);acknowledge(0);
    assert(h.rds[0].count==0);
    /* Same-mode reconfiguration cancels a not-yet-handed-off group, then CLEAR
     * plus fresh data is the authoritative Linux recovery boundary. */
    push(0);until(0,RDS_SYSTEM2);config(0,true);host_command(6,0);
    ticks(20);assert(!h.rds_tx.channel[0].pending);
    push(0);until(0,RDS_WAIT);acknowledge(0);
}
static void clear_races(void)
{
    /* CLEAR+refill during each pre-handoff operation cancels old submission. */
    for(unsigned stage=0;stage<10;++stage) {
        reset();active(0);push(0);ticks(stage);
        inject_clear=inject_push=1;tick();
        assert(h.rds[0].count==1 && !h.rds_tx.channel[0].pending);
        until(0,RDS_WAIT);acknowledge(0);assert(h.rds[0].count==0);
    }
    /* CLEAR in the handoff bus operation is after reservation: unavoidable
     * old group may finish, but never consumes a newly pushed head. */
    reset();active(0);push(0);until(0,RDS_HANDOFF);
    inject_clear=inject_push=1;tick();
    assert(h.rds_tx.channel[0].pending && h.rds[0].count==1);
    acknowledge(0);assert(h.rds[0].count==1);
    until(0,RDS_WAIT);acknowledge(0);assert(h.rds[0].count==0);
    first();inject_clear=inject_push=1;
    acknowledge(0);assert(h.rds[0].count==1);
    until(0,RDS_WAIT);acknowledge(0);assert(h.rds[0].count==0);
    first();host_command(6,0);acknowledge(0);ticks(40);
    assert(!h.qn.channel[0].rds_underruns); /* Old completion cannot arm starvation. */
}
static void failures(void)
{
    /* Fail every data/control/read stage independently. No implicit retries. */
    for(unsigned op=0;op<12;++op) {
        reset();active(0);push(0);
        ticks(op);
        if(op==11) fake_now+=RDS_POLL_US;
        fake_fail_at=fake_calls+1;fake_fail_count=1;
        tick();
        assert(h.qn.channel[0].rds_fault && h.qn.channel[0].last_error==HOST_QN_ERROR);
        assert(h.rds[0].count==1 && h.qn.channel[0].enabled && fake_cen[0]);
        unsigned writes=fake_writes;ticks(100);
        assert(fake_writes==writes && h.rds[0].count==1);
        host_command(6,0);push(0);tick();
        if(op>=10) {
            assert(h.rds_tx.channel[0].pending);
            /* Even if the failed toggle actually reached QN, acknowledgement
             * only reconciles the OLD transfer, never replays or pops new data. */
            acknowledge(0);assert(h.rds[0].count==1);
        }
        assert(!h.qn.channel[0].rds_fault);
        until(0,RDS_WAIT);acknowledge(0);assert(h.rds[0].count==0);
        assert(h.qn.channel[0].last_error==HOST_QN_ERROR); /* Latched history. */
    }
    /* Slow foreground host work can delay observation. A positive toggle on
     * the first late read is accepted; no false timeout before reading status. */
    first();fake_now+=2*RDS_ACK_TIMEOUT_US;acknowledge(0);
    assert(!h.qn.channel[0].rds_fault && !h.rds[0].count);
    first();uint8_t raw[]={1,7},out;
    assert(qn_control_execute(&h.qn,8,0,raw,&out)==HOST_OK);
    tick();assert(h.rds_tx.channel[0].raw_ambiguous);
    host_command(6,0);config(0,true);ticks(10);
    assert(h.qn.channel[0].rds_fault); /* Cannot attribute arbitrary raw toggles. */
}
static void independent(void)
{
    reset();
    for(unsigned i=0;i<8;++i) { active(i);wire[0]=(uint8_t)i;push(i); }
    for(unsigned i=0;i<8;++i) until(i,RDS_WAIT);
    for(unsigned i=0;i<8;++i) {
        assert(fake_regs[i][0x11]==i && h.rds[i].count==1);
        if(i!=3) fake_regs[i][0x1b]^=128;
    }
    fake_now+=RDS_ACK_TIMEOUT_US;ticks(10);
    assert(!h.qn.channel[3].rds_fault && h.rds[3].count==0);
    assert(h.rds_tx.channel[3].timeouts==1 && !h.rds_tx.channel[3].accepted);
    assert(!qn_control_fault(&h.qn) && qn_control_digital_mask(&h.qn)==255);
    for(unsigned i=0;i<8;++i) if(i!=3) {
        assert(!h.rds[i].count && !h.qn.channel[i].rds_fault);
        push(i);
    }
    for(unsigned i=0;i<8;++i) if(i!=3) until(i,RDS_WAIT);
    for(unsigned i=0;i<8;++i) if(i!=3) fake_regs[i][0x1b]^=128;
    fake_now+=RDS_POLL_US;ticks(10);
    for(unsigned i=0;i<8;++i) if(i!=3) assert(h.rds_tx.channel[i].accepted==2);
    host_command(4,255);assert(h.response[1]==130 && !h.response[2]);
    assert(h.response[2+10+15*3]==1 && h.response[2+10+15*3+13]==HOST_OK);
    assert(h.qn.channel[3].enabled && qn_control_led(&h.qn,0));
}
static void sustained_model(void)
{
    reset();
    for(unsigned i=0;i<8;++i) {
        active(i);
        for(unsigned n=0;n<64;++n) push(i);
    }
    uint8_t previous[8];bool owed[8]={false};
    for(unsigned i=0;i<8;++i) previous[i]=fake_regs[i][1]&4;
    const uint64_t start=fake_now;
    uint64_t slot=start+87620; /* SIMULATED encoder slots, not measured hardware. */
    while(fake_now<start+2000000) {
        for(unsigned i=0;i<8;++i) {
            uint8_t ready=fake_regs[i][1]&4;
            if(ready!=previous[i]) { assert(!owed[i]);owed[i]=true;previous[i]=ready; }
        }
        if(fake_now>=slot) {
            for(unsigned i=0;i<8;++i) if(owed[i]) { fake_regs[i][0x1b]^=128;owed[i]=false; }
            slot+=87620;
        }
        tick();
    }
    for(unsigned i=0;i<8;++i) {
        assert(h.rds_tx.channel[i].accepted>=21 && !h.qn.channel[i].rds_fault);
        assert(h.rds[i].count==64-h.rds_tx.channel[i].accepted);
    }
    printf("SIMULATED 8-channel 2 s load: accepted/TX=%lu..%lu bus_us=%llu transactions=%lu\n",
           (unsigned long)h.rds_tx.channel[0].accepted,(unsigned long)h.rds_tx.channel[7].accepted,
           (unsigned long long)h.rds_tx.bus_us,(unsigned long)h.rds_tx.transactions);
}
static void delayed_diagnostic(void)
{
    first();
    char line[320];
    assert(rds_tx_diagnostic(&h.rds_tx,line,sizeof(line)) && strstr(line,"submitted"));
    acknowledge(0);push(0);tick(); /* Next baseline is now 80, UART still pending. */
    assert(h.rds_tx.channel[0].baseline==0x80);
    assert(rds_tx_diagnostic(&h.rds_tx,line,sizeof(line)));
    assert(strstr(line,"accepted (STATUS2 bit7 changed)") && strstr(line,"baseline=00 status=80"));
}
static void startup_gate(void)
{
    reset();active(1);config(0,true);push(0);push(1);
    uint8_t on=1,off=0,out;
    assert(qn_control_execute(&h.qn,3,0,&on,&out)==HOST_OK);
    uint64_t until_us=h.qn.channel[0].rds_startup_until;
    assert(until_us==fake_now+150000);
    uint64_t before=fake_now;
    tick();
    assert(fake_now-before<2000); /* No sleep, busy-wait or blocked other TX. */
    assert(h.rds_tx.channel[0].phase==RDS_IDLE && h.rds[0].count==1);
    assert(h.rds_tx.channel[1].phase==RDS_DATA);
    until(1,RDS_WAIT);assert(!h.rds_tx.channel[0].submitted);
    host_command(1,255);assert(h.response[1]==35); /* Host remains usable. */
    fake_now=until_us-1;
    rds_tx_service(&h.rds_tx,&h.qn,h.rds);
    assert(!h.rds_tx.channel[0].submitted && !fake_regs[0][0x10]);
    fake_now=until_us;until(0,RDS_WAIT);
    assert(h.rds_tx.channel[0].submitted_at>=until_us);
    assert(h.rds_tx.channel[0].submit_tx_age_us>=150000);
    acknowledge(0);assert(!h.rds[0].count);
    /* Repeated ON does not postpone the gate. OFF/ON and reconfiguration do. */
    assert(qn_control_execute(&h.qn,3,0,&on,&out)==HOST_OK);
    assert(h.qn.channel[0].rds_startup_until==until_us);
    assert(qn_control_execute(&h.qn,3,0,&off,&out)==HOST_OK);
    assert(!h.qn.channel[0].rds_startup_until);
    assert(qn_control_execute(&h.qn,3,0,&on,&out)==HOST_OK);
    assert(h.qn.channel[0].rds_startup_until==fake_now+150000);
    push(0);host_command(6,0);fake_now+=150000;tick();
    assert(!h.rds[0].count && !h.qn.channel[0].rds_underruns);
    const uint8_t p[]={0xcc,0x55,1,0,225,0,1,50,0,1,0,0,0,0,0};
    assert(qn_control_execute(&h.qn,2,0,p,&out)==HOST_OK);
    assert(h.qn.channel[0].enabled && h.qn.channel[0].rds_startup_until==fake_now+150000);
    /* RDS disabled -> enabled while ON also uses a new deadline. */
    uint8_t disabled[15];memcpy(disabled,p,15);disabled[9]=0;
    assert(qn_control_execute(&h.qn,2,0,disabled,&out)==HOST_OK);
    assert(qn_control_execute(&h.qn,2,0,p,&out)==HOST_OK);
    push(0);before=fake_now;tick();
    assert(fake_now-before<2000 && h.rds_tx.channel[0].phase==RDS_IDLE);
}
static void timeout_recovery(void)
{
    first();
    uint8_t later[8]={0x78,0x56,0,0,0,0,0,0};
    assert(rds_fifo_push(&h.rds[0],later,1));
    fake_now+=RDS_ACK_TIMEOUT_US;tick();
    rds_tx_channel_t *s=&h.rds_tx.channel[0];
    assert(s->phase==RDS_RESET && s->timeouts==1 && !s->accepted);
    assert(h.rds[0].count==1 && !h.qn.channel[0].rds_fault);
    assert(h.qn.channel[0].enabled && !h.qn.channel[0].rds_underruns);
    host_command(4,255);assert(h.response[2+10]==1 && !h.response[2+10+13]);
    reset_system1=fake_regs[0][0];observe_reset=true;tick();observe_reset=false;
    assert(saw_rds_off && fake_regs[0][0]==reset_system1 && s->phase==RDS_RESYNC);
    fake_regs[0][0x1b]=0x80; /* A transition before resync becomes the new baseline. */
    tick();assert(s->phase==RDS_BACKOFF && s->baseline==0x80);
    unsigned calls=fake_calls;uint64_t end=s->next_poll;
    tick();assert(fake_calls==calls); /* Backoff performs no bus work or sleep. */
    host_command(1,255);assert(h.response[1]==35);
    fake_now=end;until(0,RDS_WAIT);
    assert(fake_regs[0][0x10]==0x56 && fake_regs[0][0x11]==0x78);
    assert(s->baseline==0x80 && !s->accepted && h.rds[0].count==1);
    ticks(15);assert(!s->accepted); /* Resync value itself is not a new ACK. */
    acknowledge(0);assert(s->accepted==1 && !h.rds[0].count);
    ticks(15);assert(h.qn.channel[0].rds_underruns==1);

    /* CLEAR during recovery never discards a new generation a second time. */
    first();fake_now+=RDS_ACK_TIMEOUT_US;tick();host_command(6,0);push(0);
    until(0,RDS_BACKOFF);fake_now+=RDS_RECOVERY_BACKOFF_US;until(0,RDS_WAIT);
    assert(h.rds[0].count==1);acknowledge(0);assert(!h.rds[0].count);

    /* Repeated absent ACKs are bounded losses, not starvation/QN faults. */
    first();
    for(unsigned n=1;n<=4;++n) {
        fake_now+=RDS_ACK_TIMEOUT_US;tick();until(0,RDS_BACKOFF);
        assert(h.rds_tx.channel[0].timeouts==n && !h.qn.channel[0].rds_fault);
        assert(!h.rds[0].count && !h.qn.channel[0].rds_underruns);
        push(0);fake_now+=RDS_RECOVERY_BACKOFF_US;until(0,RDS_WAIT);
    }
    /* All five SYSTEM1 operations and STATUS2 resync fail closed on bus error. */
    for(unsigned op=0;op<6;++op) {
        first();fake_now+=RDS_ACK_TIMEOUT_US;tick();
        fake_fail_at=fake_calls+op+1;fake_fail_count=1;
        tick();if(op==5) tick();
        assert(h.rds_tx.channel[0].phase==RDS_FAULT);
        assert(h.qn.channel[0].fault && h.qn.channel[0].last_error==HOST_QN_ERROR);
        assert(!h.qn.channel[0].enabled && !fake_cen[0]);
        assert(fake_cen[1] && !h.qn.channel[1].fault);
        assert(!h.rds[0].count && !h.rds_tx.channel[0].accepted);
    }
}
int main(void)
{
    exact_order();starvation_and_lifecycle();clear_races();failures();independent();sustained_model();delayed_diagnostic();startup_gate();timeout_recovery();
    printf("RDS polling: byte order, XOR, acceptance, lifecycle, IRQ CLEAR races, faults, fairness/status PASS; FIFO=%zu consumer=%zu bytes\n",
           sizeof(h.rds),sizeof(h.rds_tx));
}
