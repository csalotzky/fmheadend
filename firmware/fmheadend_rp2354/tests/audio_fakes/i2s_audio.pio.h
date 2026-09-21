#pragma once
#include "hardware/pio.h"
static const pio_program_t i2s_audio_program={8};
static inline pio_sm_config i2s_audio_program_get_default_config(unsigned offset)
{ (void)offset;return (pio_sm_config){0}; }
