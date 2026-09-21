#include "spi_stream.h"
#include "audio_samples.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static spi_stream_t s;
static uint32_t out[AUDIO_BUFFER_WORDS];
static void frame(uint32_t counter, size_t bytes, uint32_t bits, bool fault, uint64_t now)
{
    uint8_t *p=spi_stream_capture(&s);
    memset(p,0,SPI_CAPTURE_BYTES);
    for (unsigned b=0;b<4;++b) p[b]=counter>>(8*b);
    for (unsigned lane=0;lane<8;++lane) for (unsigned f=0;f<128;++f) for (unsigned lr=0;lr<2;++lr) {
        uint16_t v=(uint16_t)(0x8000+lane*1000+f*2+lr);
        unsigned offset=4+lane*512+f*4+lr*2;
        p[offset]=v; p[offset+1]=v>>8;
    }
    spi_stream_complete(&s,bytes,bits,fault,now);
}
static void good(uint32_t counter) { frame(counter,4100,32800,false,1000); }
static uint16_t sample(unsigned f,unsigned lr,unsigned lane)
{
    uint16_t v=0;
    for (unsigned bit=0;bit<16;++bit)
        v=(uint16_t)((v<<1)|((out[f*8+lr*4+bit/4]>>(lane+8*(bit%4)))&1));
    return v;
}
int main(void)
{
    spi_stream_init(&s);
    for(unsigned i=0;i<100;++i) assert(!spi_stream_take(&s,i*3000));
    assert(!spi_stream_errors(&s)); /* Never-connected source is intentional silence. */
    good(0); assert(!spi_stream_take(&s,1000)); good(1);
    const uint8_t *pcm=spi_stream_take(&s,1000); assert(pcm && pcm[0]==0);
    unsigned phase=0;
    audio_samples_mix(out,128,0,255,pcm,&phase);
    for(unsigned lane=0;lane<8;++lane) for(unsigned f=0;f<128;++f) for(unsigned lr=0;lr<2;++lr)
        assert(sample(f,lr,lane)==(uint16_t)(0x8000+lane*1000+f*2+lr));
    assert(phase==32);
    /* Borrow remains immutable while IRQ publishes/fills other slots. */
    good(2); assert(pcm[0]==0 && spi_stream_capture(&s)!=pcm);
    phase=0; audio_samples_mix(out,128,4,251,pcm,&phase);
    for(unsigned f=0;f<128;++f) for(unsigned lr=0;lr<2;++lr) {
        assert(sample(f,lr,2)==(uint16_t)audio_sine_sample(f));
        assert(sample(f,lr,1)==(uint16_t)(0x8000+1000+f*2+lr));
    }
    phase=32;audio_samples_mix(out,128,4,0,NULL,&phase);
    for(unsigned f=0;f<128;++f) {
        assert(sample(f,0,2)==(uint16_t)audio_sine_sample(f+32));
        assert(sample(f,0,1)==0); /* ANALOG/no fresh DIGITAL. */
    }
    spi_stream_release(&s);
    assert(spi_stream_take(&s,1000)[0]==1);spi_stream_release(&s);
    assert(spi_stream_take(&s,1000)[0]==2);spi_stream_release(&s);
    assert(!spi_stream_errors(&s));
    assert(!spi_stream_take(&s,1000) && spi_stream_errors(&s)==1);
    for(unsigned i=0;i<100;++i) assert(!spi_stream_take(&s,1000));
    assert(spi_stream_errors(&s)==1);
    good(3);good(4);assert(spi_stream_take(&s,1000)[0]==3);spi_stream_release(&s);
    assert(atomic_load(&s.events)&SPI_RECOVERED);
    /* Every invalid transaction flushes older queued audio; two subsequent
     * complete transactions recover, without a reset or stale replay. */
    for(unsigned kind=0;kind<7;++kind) {
        spi_stream_init(&s);good(100);good(101);
        unsigned event;
        switch(kind) {
        case 0: frame(102,4099,32792,false,1000);event=SPI_LENGTH;break;
        case 1: frame(102,4101,32808,false,1000);event=SPI_LENGTH;break;
        case 2: frame(102,4100,32801,false,1000);event=SPI_LENGTH;break;
        case 3: frame(102,4100,32800,true,1000);event=SPI_RECEIVE_ERROR;break;
        case 4: good(105);event=SPI_GAP;break;
        case 5: good(101);event=SPI_DUPLICATE;break;
        default: good(0);event=SPI_BACKWARD;break;
        }
        assert(spi_stream_errors(&s)==1 && (atomic_load(&s.events)&event));
        assert(!spi_stream_take(&s,1000));
        uint32_t next=s.previous+1; good(next); good(next+1);
        assert(spi_stream_take(&s,1000));spi_stream_release(&s);
        assert(spi_stream_errors(&s)==1);
    }
    spi_stream_init(&s);good(UINT32_MAX);good(0);good(1);
    assert(!spi_stream_errors(&s)); assert(spi_stream_take(&s,1000));
    good(2); assert(spi_stream_errors(&s)==1 && (atomic_load(&s.events)&SPI_QUEUE_FULL));
    spi_stream_release(&s);assert(!spi_stream_take(&s,1000));
    good(3);good(4);assert(spi_stream_take(&s,1000));spi_stream_release(&s);
    spi_stream_init(&s);s.read=s.write=UINT32_MAX;good(0);good(1);
    assert(spi_stream_take(&s,1000)[0]==0);spi_stream_release(&s);
    assert(spi_stream_take(&s,1000)[0]==1);spi_stream_release(&s);
    assert(s.read==1 && s.write==1);
    spi_stream_init(&s);good(0);good(1);
    assert(!spi_stream_take(&s,22000) && spi_stream_errors(&s)==1);
    atomic_store(&s.errors,UINT32_MAX);spi_stream_error(&s,SPI_LENGTH);
    assert(spi_stream_errors(&s)==UINT32_MAX);
    /* Exhaust every signed sample bit pattern against the wire decoder. */
    uint8_t packed[4100]={0};
    for(unsigned value=0;value<65536;++value) {
        for(unsigned lane=0;lane<8;++lane) {
            unsigned offset=4+512*lane;
            packed[offset]=packed[offset+2]=value;
            packed[offset+1]=packed[offset+3]=value>>8;
        }
        phase=0;audio_samples_mix(out,1,0,255,packed,&phase);
        for(unsigned lane=0;lane<8;++lane)
            assert(sample(0,0,lane)==value && sample(0,1,lane)==value);
    }
    puts("SPI frames, routes, sources, ownership, recovery and counters OK");
}
