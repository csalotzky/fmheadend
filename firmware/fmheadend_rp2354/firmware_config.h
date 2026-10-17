#ifndef FMHEADEND_FIRMWARE_CONFIG_H
#define FMHEADEND_FIRMWARE_CONFIG_H
/* Hardware population is contiguous from TX0. Storage/output lanes stay eight. */
#define FMHEADEND_FIRMWARE_VERSION "1.0.0"
#ifndef DEBUG_UART_ENABLED
#define DEBUG_UART_ENABLED 0
#endif
#if DEBUG_UART_ENABLED != 0 && DEBUG_UART_ENABLED != 1
#error "DEBUG_UART_ENABLED must be 0 or 1"
#endif
#ifndef TX_COUNT
#define TX_COUNT 8
#endif
#ifndef FRAMES_PER_PACKET
#define FRAMES_PER_PACKET 128
#endif
#if TX_COUNT < 1 || TX_COUNT > 8
#error "TX_COUNT must be 1..8"
#endif
#if FRAMES_PER_PACKET < 1 || FRAMES_PER_PACKET > 128
#error "FRAMES_PER_PACKET must be 1..128"
#endif
#define AUDIO_LANES 8u
#define AUDIO_MAX_FRAMES 128u
#define PCM_BLOCK_BYTES (FRAMES_PER_PACKET * 4u)
#define PCM_INTERNAL_BYTES (4u + AUDIO_LANES * PCM_BLOCK_BYTES)
#define TX_PRESENT_MASK ((1u << TX_COUNT) - 1u)
#endif
