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
    unsigned pos = *phase;
    for (size_t f=0; f<frames; ++f) {
        uint16_t sample = (uint16_t)sine[pos];
        for (unsigned w=0; w<4; ++w) {
            uint32_t packed=0;
            for (unsigned b=0; b<4; ++b)
                if (sample & (1u << (15-4*w-b))) packed |= (uint32_t)mask << (8*b);
            out[w]=out[w+4]=packed; /* Identical left/right. */
        }
        out += AUDIO_WORDS_PER_FRAME;
        if (++pos == 48) pos=0;
    }
    *phase=pos;
}
uint32_t audio_clock_divider256(uint32_t system_hz)
{
    uint64_t scaled=(uint64_t)system_hz*256u;
    uint64_t div=scaled/AUDIO_SM_HZ;
    return scaled%AUDIO_SM_HZ || div<256 || div>0xffffff ? 0 : (uint32_t)div;
}
