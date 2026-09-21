#include "audio_samples.h"

/* round(32767 * 10^(-12/20) * sin(2*pi*n/48)); peak 8231. */
static const int16_t sine[48] = {
    0,1074,2130,3150,4115,5011,5820,6530,7128,7604,7950,8160,
    8231,8160,7950,7604,7128,6530,5820,5011,4115,3150,2130,1074,
    0,-1074,-2130,-3150,-4115,-5011,-5820,-6530,-7128,-7604,-7950,-8160,
    -8231,-8160,-7950,-7604,-7128,-6530,-5820,-5011,-4115,-3150,-2130,-1074
};
int16_t audio_sine_sample(unsigned phase) { return sine[phase % 48]; }
void audio_samples_fill(uint32_t *out, size_t frames, uint8_t mask, unsigned *phase)
{
    audio_samples_mix(out,frames,mask,0,NULL,phase);
}
void audio_samples_mix(uint32_t *out, size_t frames, uint8_t sine_mask,
                       uint8_t digital_mask, const uint8_t *spi, unsigned *phase)
{
    unsigned pos=*phase;
    for (size_t f=0; f<frames; ++f) {
        for (unsigned slot=0;slot<2;++slot) {
            uint32_t words[4]={0};
            for (unsigned lane=0;lane<8;++lane) {
                uint16_t sample=0;
                if (sine_mask&(1u<<lane)) sample=(uint16_t)sine[pos];
                else if (spi && f<128 && (digital_mask&(1u<<lane))) {
                    const uint8_t *v=spi+4+lane*512+f*4+slot*2;
                    sample=(uint16_t)((unsigned)v[0]|(unsigned)v[1]<<8);
                }
                for (unsigned w=0;w<4;++w) {
                    uint32_t nibble=(sample>>(12-4*w))&15u;
                    /* Deposit nibble bits 3,2,1,0 into byte bits 0,8,16,24.
                     * Four parallel time steps per word; avoids 16 branches
                     * per lane/sample in the 2.667 ms refill deadline. */
                    uint32_t spread=((nibble*0x08040201u)>>3)&0x01010101u;
                    words[w] |= spread<<lane;
                }
            }
            for (unsigned w=0;w<4;++w) *out++=words[w];
        }
        if (++pos==48) pos=0;
    }
    *phase=pos;
}

uint32_t audio_clock_divider256(uint32_t system_hz)
{
    uint64_t scaled=(uint64_t)system_hz*256u;
    uint64_t div=scaled/AUDIO_SM_HZ;
    return scaled%AUDIO_SM_HZ || div<256 || div>0xffffff ? 0 : (uint32_t)div;
}
