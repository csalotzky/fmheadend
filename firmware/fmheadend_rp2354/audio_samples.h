#ifndef FMHEADEND_AUDIO_SAMPLES_H
#define FMHEADEND_AUDIO_SAMPLES_H
#include <stddef.h>
#include <stdint.h>

#define AUDIO_SAMPLE_RATE 48000u
#define AUDIO_WORDS_PER_FRAME 8u
#define AUDIO_BUFFER_FRAMES 128u
#define AUDIO_BUFFER_WORDS (AUDIO_BUFFER_FRAMES * AUDIO_WORDS_PER_FRAME)
#define AUDIO_SM_HZ (AUDIO_SAMPLE_RATE * 128u)

/* Four chronological lane bytes per word, consumed by right-shifting PIO.
 * The phase advances even during silence. No per-channel sample buffers. */
int16_t audio_sine_sample(unsigned phase);
void audio_samples_fill(uint32_t *out, size_t frames, uint8_t sine_mask, unsigned *phase);
/* Exact average frequency only; reject unrepresentable system clocks. */
uint32_t audio_clock_divider256(uint32_t system_hz);
#endif
