#ifndef FMHEADEND_SPI_AUDIO_H
#define FMHEADEND_SPI_AUDIO_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
bool spi_audio_init(void); /* Core 0 boot, before audio engine launches core 1. */
bool spi_audio_stop_for_update(void);
void spi_audio_start(void); /* Core 1: GPIO CS IRQ belongs to this core. */
void spi_audio_poll(void); /* Core 1: bounded stuck-CS/error recovery. */
const uint8_t *spi_audio_take(void);
void spi_audio_release(void);
uint32_t spi_audio_error_count(void);
void spi_audio_diagnostics(uint32_t counts[8]);
/* Core 0: aggregate exceptional events, no per-frame UART. */
bool spi_audio_diagnostic(char *line, size_t capacity, uint64_t now);
#endif
