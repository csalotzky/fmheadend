#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "host_test_frame.h"
#include "fake_qn.h"

static host_protocol_t host;
static void request(const uint8_t *bytes, unsigned length, uint8_t expected)
{
    const unsigned calls=fake_calls;
    test_receive(&host,bytes,length);
    host_protocol_stop(&host,0);
    assert(!host.pending && !host.running); /* Immediate response; no worker/wait. */
    assert(fake_calls==calls); /* FIFO commands never touch QNs. */
    assert(host_protocol_read(&host)==expected);
    const uint8_t size=host_protocol_read(&host);
    assert(size==(expected==HOST_OK && bytes[0]==11 ? 16 : 0));
    host_protocol_stop(&host,0); /* Read STOP preserves payload cursor. */
    test_response_crc(&host);
    for (unsigned i=0;i<size+2u;++i) assert(host_protocol_read(&host)==host.response[2+i]);
    assert(host_protocol_read(&host)==0xff);
    host_log_event_t event;
    while(host_protocol_pop_log(&host,&event)) assert(event.status==expected);
}
static void push(unsigned tx, unsigned count, uint8_t expected)
{
    uint8_t bytes[252] = {5,tx,1+8*count,count};
    for (unsigned i=0;i<count*8;++i) bytes[4+i]=(uint8_t)(tx*19+i*37);
    request(bytes,4+8*count,expected);
}
static void free_counts(const uint16_t *expected)
{
    const uint8_t request_bytes[]={11,255,0};
    request(request_bytes,sizeof(request_bytes),HOST_OK);
    assert(host.response_size==20);
    for (unsigned i=0;i<8;++i) {
        assert(host.response[2+2*i]==expected[i] && host.response[3+2*i]==0);
        assert(rds_fifo_free(&host.rds[i])==expected[i]);
    }
}
static void status_counts(const uint16_t *expected)
{
    const uint8_t bytes[]={4,255,0};
    test_receive(&host,bytes,3);
    host_protocol_stop(&host,0);
    host_work_t work;
    assert(host_protocol_take_work(&host,&work));
    host_protocol_run_work(&host,&work,123);
    host_protocol_complete_work(&host,&work);
    assert(host.response[0]==HOST_OK && host.response[1]==130);
    for (unsigned i=0;i<8;++i) {
        const uint8_t *channel=host.response+2+10+15*i;
        assert(host_get_u16(channel+11)==expected[i]);
        assert(host_get_u32(channel+7)==0); /* No RDS consumer -> no underruns. */
        assert(channel[0]==0); /* RF stays OFF. */
    }
    host_log_event_t event;
    assert(host_protocol_pop_log(&host,&event) && event.status==HOST_OK);
}
int main(void)
{
    fake_qn_reset();host_protocol_init(&host,0xff);
    uint16_t expected[8]={64,64,64,64,64,64,64,64};
    free_counts(expected);status_counts(expected);
    qn_control_t qn_before=host.qn;
    for(unsigned tx=0;tx<8;++tx) { push(tx,tx+1,HOST_OK);expected[tx]-=tx+1; }
    free_counts(expected);
    const uint8_t clear3[]={6,3,0};
    request(clear3,sizeof(clear3),HOST_OK);expected[3]=64;
    free_counts(expected); /* CLEAR leaves all seven other nonempty queues alone. */
    push(3,4,HOST_OK);expected[3]=60;
    for(unsigned tx=0;tx<8;++tx) for(unsigned n=0;n<=tx;++n) {
        rds_group_t group;
        assert(rds_fifo_pop(&host.rds[tx],&group));
        for(unsigned b=0;b<4;++b) {
            unsigned i=n*8+2*b;
            assert(group.blocks[b]==((uint8_t)(tx*19+i*37) |
                   (uint16_t)(uint8_t)(tx*19+(i+1)*37)<<8));
        }
    }
    for(unsigned tx=0;tx<8;++tx) expected[tx]=64;
    push(0,31,HOST_OK);push(0,31,HOST_OK);expected[0]=2;
    rds_fifo_t before[8];memcpy(before,host.rds,sizeof(before));
    push(0,3,HOST_BUSY);
    assert(!memcmp(before,host.rds,sizeof(before)));
    free_counts(expected);status_counts(expected);
    push(0,2,HOST_OK);expected[0]=0;
    free_counts(expected);push(0,1,HOST_BUSY);
    const uint8_t clear[]={6,0,0};request(clear,sizeof(clear),HOST_OK);
    expected[0]=64;free_counts(expected);status_counts(expected);
    assert(!memcmp(&qn_before,&host.qn,sizeof(qn_before)));

    /* Validate complete framing, count, target and payload before any append. */
    const uint8_t bad[][12]={
        {5,0,0}, {5,0,1,0}, {5,0,1,32}, {5,0,1,255},
        {5,0,1,1}, {5,0,9,2}, {5,8,9,1}, {5,255,9,1},
        {6,0,1,0}, {6,8,0}, {6,255,0}, {11,0,0}, {11,255,1,0}
    };
    for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);++i) {
        memcpy(before,host.rds,sizeof(before));
        request(bad[i],3+bad[i][2],i==6 || i==7 || (i>=9 && i<=11) ?
                HOST_INVALID_TARGET : HOST_INVALID_ARGUMENT);
        assert(!memcmp(before,host.rds,sizeof(before)));
    }
    const uint8_t truncated[]={5,0,9,1,0,0};
    request(truncated,sizeof(truncated),HOST_INVALID_ARGUMENT);
    const uint8_t extra[]={5,0,9,1,0,0,0,0,0,0,0,0,42};
    request(extra,sizeof(extra),HOST_INVALID_ARGUMENT);
    /* Repeated free-space polls are quiet and never fill the diagnostic queue. */
    const uint8_t free_req[]={11,255,0};
    for(unsigned n=0;n<100;++n) {
        test_receive(&host,free_req,3);
        host_protocol_stop(&host,0);assert(host.response[0]==HOST_OK);
    }
    assert(host.log_count==0);
    /* Backpressure never partially appends, and invalid requests stay invalid. */
    memcpy(before,host.rds,sizeof(before));
    host.running=true;
    const uint8_t one[]={5,0,9,1,0,0,0,0,0,0,0,0};
    test_receive(&host,one,sizeof(one));
    host_protocol_stop(&host,0);
    assert(host.response[0]==HOST_BUSY && !memcmp(before,host.rds,sizeof(before)));
    host.running=false;
    host_log_event_t event;
    assert(host_protocol_pop_log(&host,&event) && event.status==HOST_BUSY);
    host.log_count=HOST_LOG_CAPACITY;
    test_receive(&host,one,sizeof(one));
    host_protocol_stop(&host,0);
    assert(host.response[0]==HOST_BUSY && !memcmp(before,host.rds,sizeof(before)));
    test_receive(&host,truncated,sizeof(truncated));
    host_protocol_stop(&host,0);
    assert(host.response[0]==HOST_INVALID_ARGUMENT && !memcmp(before,host.rds,sizeof(before)));
    /* No configuration, RF or boot-readiness prerequisite for RAM transport. */
    host_protocol_init(&host,0);
    push(7,31,HOST_OK);expected[7]=33;free_counts(expected);
    for(unsigned n=0;n<31;++n) {
        rds_group_t group;
        assert(rds_fifo_pop(&host.rds[7],&group));
        for(unsigned b=0;b<4;++b) {
            unsigned i=n*8+2*b;
            assert(group.blocks[b]==((uint8_t)(7*19+i*37) |
                   (uint16_t)(uint8_t)(7*19+(i+1)*37)<<8));
        }
    }
    assert(host_response_wait_ms(5)==2 && host_response_wait_ms(6)==2 && host_response_wait_ms(11)==2);
    puts("RDS V1: all targets, full payload, atomic BUSY, validation, 16-byte free, 130-byte status, no QN work PASS");
}
