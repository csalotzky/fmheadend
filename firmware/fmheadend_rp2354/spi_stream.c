#include "spi_stream.h"
#include <string.h>
#include <assert.h>
static uint32_t le32(const uint8_t *p)
{ return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24; }
void spi_stream_init(spi_stream_t *s)
{ memset(s,0,sizeof(*s)); atomic_init(&s->errors,0); atomic_init(&s->events,0); }
uint8_t *spi_stream_capture(spi_stream_t *s) { return s->frames[s->write%SPI_FRAME_SLOTS]; }
uint32_t spi_stream_errors(const spi_stream_t *s) { return atomic_load(&s->errors); }
void spi_stream_error(spi_stream_t *s, unsigned event)
{
    /* Only core 1 writes, with its CS IRQ masked in foreground. */
    unsigned errors=atomic_load(&s->errors);
    if (errors!=UINT32_MAX) atomic_store(&s->errors,errors+1);
    atomic_fetch_or(&s->events,event);
    s->flush_before=s->write; s->reset_pending=true;
}
void spi_stream_complete(spi_stream_t *s, size_t bytes, uint32_t bits, bool hw_error, uint64_t now)
{
    if (hw_error || bytes!=SPI_FRAME_BYTES || bits!=8*SPI_FRAME_BYTES) {
        spi_stream_error(s,hw_error ? SPI_RECEIVE_ERROR : SPI_LENGTH);
        s->have_counter=false; /* Rebase after an untrustworthy header. */
        return;
    }
    uint32_t counter=le32(spi_stream_capture(s));
    uint32_t delta=counter-s->previous;
    bool discontinuity=s->have_counter && delta!=1;
    s->previous=counter; s->have_counter=true;
    if (discontinuity) {
        spi_stream_error(s,!delta ? SPI_DUPLICATE : delta<0x80000000u ? SPI_GAP : SPI_BACKWARD);
        return; /* Reject this block; the following sequential frame can recover. */
    }
    if (s->write-s->read>=SPI_FRAME_SLOTS-1) {
        spi_stream_error(s,SPI_QUEUE_FULL); return;
    }
    /* Complete the fixed eight-lane PCM layout before publishing the slot.
     * Bytes left by an earlier capture must never reach a missing TX. */
    memset(spi_stream_capture(s)+SPI_FRAME_BYTES,0,PCM_INTERNAL_BYTES-SPI_FRAME_BYTES);
    s->received[s->write%SPI_FRAME_SLOTS]=now;
    ++s->write;
    if (!s->seen_valid) { s->seen_valid=true; atomic_fetch_or(&s->events,SPI_FIRST); }
}
const uint8_t *spi_stream_take(spi_stream_t *s, uint64_t now)
{
    assert(!s->borrowed);
    if (s->reset_pending) {
        s->read=s->flush_before; s->reset_pending=false;
        if (s->playing) atomic_fetch_or(&s->events,SPI_LOST);
        s->playing=false;
    }
    unsigned queued=s->write-s->read;
    if (queued && now-s->received[s->read%SPI_FRAME_SLOTS]>SPI_MAX_AGE_US) {
        spi_stream_error(s,SPI_LOST); s->read=s->write; s->reset_pending=false;
        s->playing=false; return NULL;
    }
    if (!s->playing) {
        if (queued<SPI_PREFILL) return NULL;
        s->playing=true;
        if (s->seen_playback) atomic_fetch_or(&s->events,SPI_RECOVERED);
        s->seen_playback=true;
    }
    if (!queued) {
        spi_stream_error(s,SPI_LOST); s->playing=false; return NULL;
    }
    s->borrowed=true;
    return s->frames[s->read%SPI_FRAME_SLOTS];
}
void spi_stream_release(spi_stream_t *s)
{ if (s->borrowed) { ++s->read; s->borrowed=false; } }
