#ifndef FMHEADEND_AUDIO_ENGINE_H
#define FMHEADEND_AUDIO_ENGINE_H
#include <stdbool.h>
#include <stdint.h>
typedef enum {
    AUDIO_STARTING, AUDIO_RUNNING, AUDIO_RESOURCE_FAULT, AUDIO_CLOCK_FAULT,
    AUDIO_DMA_FAULT, AUDIO_STALL_FAULT, AUDIO_DEADLINE_FAULT, AUDIO_PIO_FAULT
} audio_state_t;
/* Boot once, after QNs are safe. Core 1 owns the engine thereafter. */
bool audio_engine_init(void);
bool audio_engine_enter_update(void);
void audio_engine_set_sources(uint8_t sine_mask, uint8_t digital_mask);
audio_state_t audio_engine_state(void);
const char *audio_engine_state_name(audio_state_t state);
/* Core 0 only: once per latched failure, never from an IRQ/audio refill. */
void audio_engine_log_fault(void);
#endif
