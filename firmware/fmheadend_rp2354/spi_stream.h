#ifndef FMHEADEND_SPI_STREAM_H
#define FMHEADEND_SPI_STREAM_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
#define SPI_FRAME_BYTES 4100u
#define SPI_CAPTURE_BYTES 4101u /* One extra byte detects oversize transactions. */
#define SPI_FRAME_SLOTS 4u /* Three queued/borrowed frames plus one DMA capture. */
#define SPI_PREFILL 2u
#define SPI_MAX_AGE_US 20000u
enum spi_event {
    SPI_FIRST=1, SPI_RECOVERED=2, SPI_LOST=4, SPI_LENGTH=8, SPI_GAP=16,
    SPI_DUPLICATE=32, SPI_BACKWARD=64, SPI_RECEIVE_ERROR=128, SPI_QUEUE_FULL=256
};
typedef struct {
    uint8_t frames[SPI_FRAME_SLOTS][4104];
    uint64_t received[SPI_FRAME_SLOTS];
    uint32_t write, read, flush_before, previous;
    bool have_counter, reset_pending, playing, borrowed, seen_valid, seen_playback;
    atomic_uint errors, events;
} spi_stream_t;
/* Core 1 only. Hardware adapter masks its CS IRQ around take/release/poll.
 * Producer never writes a queued/borrowed buffer. No copies or allocation. */
void spi_stream_init(spi_stream_t *s);
uint8_t *spi_stream_capture(spi_stream_t *s);
void spi_stream_complete(spi_stream_t *s, size_t bytes, uint32_t bits, bool hw_error, uint64_t now);
void spi_stream_error(spi_stream_t *s, unsigned event);
const uint8_t *spi_stream_take(spi_stream_t *s, uint64_t now);
void spi_stream_release(spi_stream_t *s);
uint32_t spi_stream_errors(const spi_stream_t *s);
#endif
