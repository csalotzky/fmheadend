#ifndef HOST_TEST_FRAME_H
#define HOST_TEST_FRAME_H
#include <assert.h>
#include "host_protocol.h"

/* Existing command tests describe header/payload; exercise CRC on the wire. */
static inline void test_receive(host_protocol_t *h, const uint8_t *body, size_t n)
{
    uint16_t crc = host_crc16(body, n);
    for (size_t i = 0; i < n; ++i) host_protocol_receive(h, body[i], i == 0);
    host_protocol_receive(h, (uint8_t)crc, false);
    host_protocol_receive(h, (uint8_t)(crc >> 8), false);
}
static inline void test_response_crc(const host_protocol_t *h)
{
    assert(h->response_size == 4u + h->response[1]);
    assert(host_get_u16(h->response + h->response_size - 2) ==
           host_crc16(h->response, h->response_size - 2));
}
#endif
