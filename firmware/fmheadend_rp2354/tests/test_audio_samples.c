#include "audio_samples.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
static int16_t decode(const uint32_t *words,unsigned lane)
{
    uint16_t sample=0;
    for (unsigned b=0;b<16;++b) sample=(sample<<1)|((words[b/4]>>(8*(b%4)+lane))&1);
    return (int16_t)sample;
}
int main(void)
{
    assert(AUDIO_SAMPLE_RATE==48000 && AUDIO_SAMPLE_RATE/48==1000);
    double energy=0;
    for(unsigned i=0;i<48;++i) {
        double expected=32767*pow(10,-12.0/20)*sin(2*3.141592653589793*i/48);
        assert(fabs(audio_sine_sample(i)-expected)<=0.5);
        assert(audio_sine_sample(i)==-audio_sine_sample((i+24)%48));
        energy+=(double)audio_sine_sample(i)*audio_sine_sample(i);
    }
    assert(fabs(20*log10(sqrt(energy/24)/32767)+12)<0.001);
    assert(audio_clock_divider256(150000000)==6250);
    assert(audio_clock_divider256(125000000)==0);
    assert((uint64_t)150000000*256/6250/128==48000);
    uint32_t out[AUDIO_BUFFER_WORDS], continuous[3*AUDIO_BUFFER_WORDS];
    unsigned phase=0, other=0;
    audio_samples_fill(continuous,3*AUDIO_BUFFER_FRAMES,0xa5,&other);
    for(unsigned chunk=0;chunk<3;++chunk) {
        audio_samples_fill(out,AUDIO_BUFFER_FRAMES,0xa5,&phase);
        assert(!memcmp(out,continuous+chunk*AUDIO_BUFFER_WORDS,sizeof(out)));
    }
    assert(phase==other && phase==0); /* 384 frames = 8 periods; 128 isn't a period. */
    for(unsigned mask=0;mask<256;++mask) {
        unsigned start=phase;
        audio_samples_fill(out,AUDIO_BUFFER_FRAMES,mask,&phase);
        for(unsigned f=0;f<AUDIO_BUFFER_FRAMES;++f) for(unsigned lane=0;lane<8;++lane) {
            int16_t want=(mask&(1u<<lane)) ? audio_sine_sample(start+f) : 0;
            assert(decode(out+8*f,lane)==want && decode(out+8*f+4,lane)==want);
        }
    }
    puts("48 kHz / 1 kHz / -12 dBFS, MSB-first packing, all lane masks and DMA boundaries PASS");
}
