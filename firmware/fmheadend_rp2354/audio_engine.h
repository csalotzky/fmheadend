#ifndef FMHEADEND_AUDIO_ENGINE_H
#define FMHEADEND_AUDIO_ENGINE_H
#include <stdbool.h>
#include <stdint.h>
typedef enum {
    AUDIO_STARTING, AUDIO_RUNNING, AUDIO_RESOURCE_FAULT, AUDIO_CLOCK_FAULT,
    AUDIO_DMA_FAULT, AUDIO_STALL_FAULT, AUDIO_DEADLINE_FAULT
} audio_state_t;
/* Boot once, after QNs are safe. Core 1 owns the engine thereafter. */
bool audio_engine_init(void);
void audio_engine_set_sine_mask(uint8_t mask);
audio_state_t audio_engine_state(void);
const char *audio_engine_state_name(audio_state_t state);
#endif
