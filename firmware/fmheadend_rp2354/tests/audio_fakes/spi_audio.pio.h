#pragma once
#include "hardware/pio.h"
static const pio_program_t spi_audio_program={5};
static inline pio_sm_config spi_audio_program_get_default_config(unsigned off) { (void)off;return (pio_sm_config){0}; }
