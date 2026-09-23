#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "host_protocol.h"
#include "qn_control.h"
#include "fake_qn.h"
#include "board.h"
static qn_control_t q;
static uint8_t published_mask, digital_mask;
static unsigned notifications;
static void source_sink(uint8_t mask, uint8_t digital) { digital_mask=digital; published_mask=mask; ++notifications; }
static uint8_t config[15]={0x34,0x7f,1,0,60,0,1,50,0,0,1,0xf1,0xff,1,1};
static void reset(void)
{ fake_qn_reset(); qn_control_init(&q,255); qn_control_set_audio_sink(&q,source_sink); notifications=0; }
static uint8_t call(unsigned cmd,unsigned ch,const uint8_t *p)
{ uint8_t out; return qn_control_execute(&q,cmd,ch,p,&out); }
static void set_frequency(uint8_t *p,unsigned freq)
{ for(unsigned b=0;b<4;++b) p[b]=freq>>(8*b); }
int main(void)
{
    reset(); assert(!qn_control_active(&q) && !qn_control_led(&q,300000));
    for (unsigned i=0;i<8;++i) {
        uint8_t r[]={6},w[]={0x30,(uint8_t)(i+40)},v=0;
        assert(qn_control_execute(&q,7,i,r,&v)==0 && v==0x11);
        assert(call(8,i,w)==0 && fake_regs[i][0x30]==i+40);
    }
    uint8_t on[]={1},off[]={0};
    assert(call(3,0,on)==HOST_INVALID_ARGUMENT);
    assert(call(3,0,off)==0); /* OFF needs no configuration. */
    for(unsigned mode=0;mode<3;++mode) {
        reset(); config[8]=mode;
        assert(call(2,3,config)==0);
        assert(fake_regs[3][7]==0x71 && fake_regs[3][4]==QN_REG_VGA_DEFAULT);
        assert(fake_regs[3][0]==(mode==1?1:5));
        assert(fake_regs[3][8]==0xba && (fake_regs[3][11]&3)==1);
        assert(fake_regs[3][13]==0xd0 && fake_regs[3][0x5a]==0xe4);
        assert(q.channel[3].configured && !q.channel[3].enabled);
        assert(qn_control_sine_mask(&q)==0);
        assert(digital_mask==qn_control_digital_mask(&q));
        assert(digital_mask==(mode==0 ? 8 : 0));
        assert(call(3,3,on)==0 && q.channel[3].enabled);
        assert(qn_control_sine_mask(&q)==(mode==2 ? 8 : 0));
        assert(published_mask==qn_control_sine_mask(&q));
        assert(qn_control_led(&q,0));
        assert(fake_regs[3][0]==(mode==1?0x41:0x45));
        assert(call(2,3,config)==0 && q.channel[3].enabled);
        uint8_t status[130]; fake_regs[3][26]=8;
        qn_control_status(&q,status,0x78563412);
        assert(status[0]==0 && host_get_u32(status+1)==0x78563412);
        assert(status[55]==1 && host_get_i16(status+56)==60);
        assert(status[69]==(mode==1?1:255));
        assert(call(3,3,off)==0 && !qn_control_active(&q));
        assert(qn_control_sine_mask(&q)==0);
        assert(digital_mask==qn_control_digital_mask(&q));
        assert(digital_mask==(mode==0 ? 8 : 0));
    }
    /* Exactly representable frequency endpoints, every RF step and manual gain. */
    for(unsigned step=0;step<16;++step) {
        reset(); uint8_t p[15]; memcpy(p,config,15); p[4]=step*15;
        set_frequency(p,step?108000:76000);
        assert(call(2,0,p)==0 && (fake_regs[0][90]&15)==step);
        assert((fake_regs[0][90]&0x30)==0x20); /* IPOW unchanged. */
        assert(fake_regs[0][8]==(step?0x80:0) && (fake_regs[0][11]&3)==(step?2:0));
    }
    for(int gain=-15;gain<=150;gain+=15) {
        reset(); uint8_t p[15]; memcpy(p,config,15); p[11]=(uint16_t)gain;p[12]=(uint16_t)gain>>8;
        assert(call(2,0,p)==0 && (fake_regs[0][13]&15)==(unsigned)(gain+15)/15);
    }
    reset(); uint8_t p[15]; memcpy(p,config,15);p[10]=0;p[11]=255;p[12]=127;p[14]=0;
    assert(call(2,0,p)==0 && fake_regs[0][13]==1); /* AUTO ignores manual controls. */
    reset();memcpy(p,config,15);p[6]=0;p[7]=75;p[8]=1;p[9]=1;
    fake_regs[0][1]=4; /* Preserve the existing RDS-ready handshake bit. */
    assert(call(2,0,p)==0 && fake_regs[0][1]==0x1f && fake_regs[0][0]==3);
    assert(call(3,0,on)==0 && fake_regs[0][0]==0x43);
    unsigned before_invalid=fake_calls, before_notify=notifications;p[7]=51;
    assert(call(2,0,p)==HOST_INVALID_ARGUMENT && fake_calls==before_invalid);
    assert(q.channel[0].configured && q.channel[0].enabled); /* Old valid config. */
    assert(notifications==before_notify); /* Invalid config cannot glitch DATA. */
    reset();memcpy(p,config,15);p[10]=0;p[14]=1;
    assert(call(2,0,p)==0 && (fake_regs[0][13]&0xc0)==0x80); /* AUTO + clipping. */
    reset();memcpy(p,config,15);p[14]=0;p[13]=0;
    assert(call(2,0,p)==0 && fake_regs[0][13]==0x40); /* MANUAL, zero trim, no clip. */
    /* Validation happens before any QN operation. */
    for(unsigned field=0;field<15;++field) {
        reset();memcpy(p,config,15);
        if(field<4) set_frequency(p,field==0?75950:field==1?108050:98101);
        else if(field==4 || field==5) {p[4]=1;p[5]=0;}
        else if(field==7) p[7]=51;
        else if(field==8) p[8]=3;
        else if(field==11 || field==12) {p[11]=1;p[12]=0;}
        else p[field]=2;
        assert(call(2,0,p)==HOST_INVALID_ARGUMENT && !fake_calls);
    }
    /* Two retries, then a real error and CEN safe-off. */
    for(unsigned failures=0;failures<=3;++failures) {
        reset();fake_fail_at=1;fake_fail_count=failures;uint8_t r[]={6},v=0;
        uint8_t rc=qn_control_execute(&q,7,7,r,&v);
        assert(fake_calls==(failures<3?failures+1:3));
        assert(rc==(failures<3?HOST_OK:HOST_QN_ERROR));
        assert(fake_cen[7]==(failures<3));
    }
    /* Every config bus operation can fail; none leaves active partial RF. */
    reset();assert(call(2,0,config)==0);unsigned count=fake_calls;
    assert(count==28);
    for(unsigned op=1;op<=count;++op) {
        reset();assert(call(2,0,config)==0);assert(call(3,0,on)==0);
        fake_calls=0;fake_fail_at=op;fake_fail_count=3;
        assert(call(2,0,config)==HOST_QN_ERROR);
        assert(!q.channel[0].configured && !q.channel[0].enabled && !fake_cen[0]);
        assert(published_mask==0);
        assert(qn_control_fault(&q) && !qn_control_led(&q,0) && qn_control_led(&q,250000));
    }
    for(unsigned enabled=0;enabled<2;++enabled) for(unsigned op=1;op<=3;++op) {
        reset();assert(call(2,0,config)==0);assert(call(3,0,on)==0);
        fake_calls=0;fake_fail_at=op;fake_fail_count=3;
        assert(call(3,0,enabled?on:off)==HOST_QN_ERROR && !fake_cen[0]);
    }
    reset();fake_mismatch_reg=7;
    assert(call(2,0,config)==HOST_QN_ERROR && !fake_cen[0]);
    /* Every route/configuration restores the calibrated load. AUTO/MANUAL gain
     * and audio-mode changes must leave XCSEL intact, including active TXs.
     */
    assert(QN_REG_VGA_DEFAULT==0x45);
    for(unsigned i=0;i<8;++i) {
        reset(); memcpy(p,config,15); set_frequency(p,87500);
        fake_regs[i][4]=0x60;
        for(unsigned mode=0;mode<3;++mode) for(unsigned manual=0;manual<2;++manual) {
            p[8]=mode; p[10]=manual;
            assert(call(2,i,p)==HOST_OK && fake_regs[i][4]==0x45);
            assert((fake_regs[i][4]&0xc0)==0x40); /* Original RIN/gain table. */
            assert(fake_regs[i][8]==230 && (fake_regs[i][11]&3)==0);
            assert(call(3,i,on)==HOST_OK && fake_regs[i][4]==0x45);
        }
        /* A deliberate raw diagnostic override is allowed; SET restores it. */
        uint8_t override[]={4,0xe0}; assert(call(8,i,override)==HOST_OK);
        assert(fake_regs[i][4]==0xe0);
        assert(call(2,i,p)==HOST_OK && fake_regs[i][4]==0x45);
        assert(q.channel[i].enabled);
        reset(); fake_mismatch_reg=4;
        assert(call(2,i,p)==HOST_QN_ERROR && !fake_cen[i]);
    }
    /* Raw SYSTEM1 bypasses configuration prerequisite but status stays honest. */
    reset();uint8_t raw[]={0,0x41};assert(call(8,2,raw)==0 && q.channel[2].enabled);
    uint8_t status[130];qn_control_status(&q,status,1);assert(status[40]==1);
    raw[1]=1;assert(call(8,2,raw)==0 && !q.channel[2].enabled);
    raw[0]=90;raw[1]=0x47;assert(call(8,2,raw)==0 && q.channel[2].attenuation==105);
    raw[0]=2;raw[1]=0x2a;assert(call(8,2,raw)==0 && !fake_cen[2]);
    reset();raw[0]=1;raw[1]=0x80;assert(call(8,1,raw)==0 && !fake_cen[1]);
    reset();raw[0]=0;raw[1]=0x45;fake_mismatch_reg=0;
    assert(call(8,0,raw)==HOST_QN_ERROR && !fake_cen[0]);
    reset();q.last_error=q.channel[0].last_error=HOST_QN_ERROR;
    qn_control_status(&q,status,0);
    assert(status[0]==0 && status[9]==HOST_QN_ERROR && status[10]==0);
    assert(!qn_control_led(&q,250000)); /* Latched history isn't an active fault. */
    /* Timing under two full timeout failures before EACH successful access. */
    reset();fake_retry_twice=true;
    assert(call(2,0,config)==0 && fake_now<1000000);
    uint64_t before=fake_now;assert(call(3,0,on)==0 && fake_now-before<150000);
    reset();for(unsigned i=0;i<8;++i) {q.channel[i].configured=true;fake_regs[i][0]=1;}
    fake_retry_twice=true;qn_control_status(&q,status,0);assert(fake_now<850000);
    assert(q.last_error==0); /* Recovered attempts are history, not active faults. */
    reset(); config[8]=2;
    for(unsigned i=0;i<8;++i) { assert(call(2,i,config)==0); assert(call(3,i,on)==0); }
    assert(qn_control_sine_mask(&q)==255);
    config[8]=0; assert(call(2,3,config)==0 && qn_control_sine_mask(&q)==247);
    config[8]=1; assert(call(2,6,config)==0 && qn_control_sine_mask(&q)==183);
    config[8]=2; assert(call(3,0,off)==0 && qn_control_sine_mask(&q)==182);
    fake_fail_at=fake_calls+1; fake_fail_count=3;
    assert(call(2,1,config)==HOST_QN_ERROR && qn_control_sine_mask(&q)==180);
    reset(); qn_control_audio_fault(&q,true); qn_control_status(&q,status,0);
    assert(status[0]==1 && status[9]==HOST_INTERNAL_ERROR && !qn_control_led(&q,0));
    assert(qn_control_led(&q,250000));
    for(unsigned i=5;i<9;++i) assert(status[i]==0); /* No invented SPI errors. */
    for(unsigned i=0;i<8;++i) {
        assert(status[10+15*i]==0);
        for(unsigned b=3;b<13;++b) assert(status[10+15*i+b]==0);
    }
    qn_control_audio_fault(&q,false); qn_control_status(&q,status,0);
    assert(status[0]==0 && status[9]==HOST_INTERNAL_ERROR); /* Latched history. */
    puts("QN control: routes, config, RF lifecycle, source masks, real faults, LED and timing PASS");
}
