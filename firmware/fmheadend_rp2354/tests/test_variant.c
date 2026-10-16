#include "spi_stream.h"
#include "audio_samples.h"
#include "host_test_frame.h"
#include "fake_qn.h"
#include <string.h>
#include <stdio.h>
static spi_stream_t stream;
static uint32_t output[AUDIO_BUFFER_WORDS+1];
static uint16_t decode(unsigned f,unsigned lr,unsigned lane) {
    uint16_t value=0;
    for(unsigned b=0;b<16;++b)
        value=(value<<1)|((output[f*8+lr*4+b/4]>>(lane+8*(b%4)))&1);
    return value;
}
static void packet(unsigned counter) {
    uint8_t *p=spi_stream_capture(&stream);
    memset(p,0xa5,4104); /* Missing lanes contain stale data until publication. */
    memset(p,0,4);p[0]=counter;
    for(unsigned lane=0;lane<TX_COUNT;++lane)
        for(unsigned f=0;f<FRAMES_PER_PACKET;++f)
            for(unsigned lr=0;lr<2;++lr) {
                unsigned at=4+lane*PCM_BLOCK_BYTES+f*4+lr*2;
                uint16_t value=0x8000+lane*300+f*2+lr;
                p[at]=value;p[at+1]=value>>8;
            }
    spi_stream_complete(&stream,SPI_FRAME_BYTES,SPI_FRAME_BYTES*8,false,1000);
}
int main(void) {
    spi_stream_init(&stream); packet(0);packet(1);
    const uint8_t *p=spi_stream_take(&stream,1000);assert(p);
    output[AUDIO_BUFFER_WORDS]=0xdeadbeef;
    unsigned phase=0;audio_samples_mix(output,FRAMES_PER_PACKET,0,255,p,&phase);
    for(unsigned lane=0;lane<8;++lane) for(unsigned f=0;f<FRAMES_PER_PACKET;++f)
        for(unsigned lr=0;lr<2;++lr)
            assert(decode(f,lr,lane)==(lane<TX_COUNT ? (uint16_t)(0x8000+lane*300+f*2+lr) : 0));
    assert(phase==FRAMES_PER_PACKET%48 && output[AUDIO_BUFFER_WORDS]==0xdeadbeef);
    spi_stream_release(&stream);
    spi_stream_complete(&stream,SPI_FRAME_BYTES-1,SPI_FRAME_BYTES*8-8,false,1000);
    assert(spi_stream_errors(&stream)==1 && !spi_stream_take(&stream,1000));
    host_protocol_t host;host_protocol_init(&host,255);
    uint8_t info[]={1,255,0};test_receive(&host,info,sizeof(info));host_protocol_stop(&host,0);
    assert(host.response[36]==TX_COUNT);test_response_crc(&host);
    fake_qn_reset();uint8_t status[130];qn_control_status(&host.qn,status,0);
    assert(!qn_control_fault(&host.qn));
    for(unsigned i=TX_COUNT;i<8;++i) {
        unsigned calls=fake_calls;uint8_t request[]={7,i,1,0};
        test_receive(&host,request,sizeof(request));host_protocol_stop(&host,0);
        assert(host.response[0]==HOST_INVALID_TARGET && fake_calls==calls);
        assert(!host.qn.channel[i].ready && !host.qn.channel[i].fault);
    }
    printf("TX=%u frames=%u: PCM lanes, zero fill, bounds, GET_INFO and missing targets passed\n",TX_COUNT,FRAMES_PER_PACKET);
}
