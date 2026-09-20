#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "host_protocol.h"

static host_protocol_t host;
static const uint8_t config[] = {2, 3, 15, 0x34, 0x7f, 1, 0, 60, 0,
                               1, 50, 0, 0, 1, 0xf1, 0xff, 1, 1};

static void receive(const uint8_t *r, size_t n)
{
    for (size_t i = 0; i < n; ++i) host_protocol_receive(&host, r[i], i == 0);
}
static host_log_event_t request(const uint8_t *r, size_t n, uint8_t expected)
{
    receive(r, n);
    host_protocol_stop(&host, 0x78563412);
    assert(host.response[0] == expected);
    if (expected) assert(host.response_size == 2 && host.response[1] == 0);
    host_log_event_t event;
    assert(host_protocol_pop_log(&host, &event));
    assert(event.status == expected);
    return event;
}
static void status(uint8_t ready_mask)
{
    const uint8_t req[] = {4, 0xff, 0};
    request(req, sizeof(req), HOST_OK);
    assert(host.response_size == 132 && host.response[1] == 130);
    const uint8_t *p = host.response + 2;
    assert(p[0] == (ready_mask != 0xff));
    assert(host_get_u32(p + 1) == 0x78563412);
    assert(host_get_u32(p + 5) == 0 && p[9] == (ready_mask == 0xff ? 0 : 5));
    for (unsigned i = 0; i < 8; ++i) {
        const uint8_t *c = p + 10 + i * 15;
        assert(c[0] == 0 && host_get_i16(c + 1) == -32768);
        assert(host_get_u32(c + 3) == 0 && host_get_u32(c + 7) == 0);
        assert(host_get_u16(c + 11) == 0 && c[14] == 255);
        assert(c[13] == (ready_mask & (1u << i) ? 0 : 5));
    }
}
int main(void)
{
    host_protocol_init(&host, 0xff);
    assert(host_protocol_read(&host) == HOST_BUSY && host_protocol_read(&host) == 0);
    const uint8_t info[] = {1, 0xff, 0};
    request(info, sizeof(info), HOST_OK);
    assert(host.response_size == 37);
    assert(host_protocol_read(&host) == 0 && host_protocol_read(&host) == 35);
    host_protocol_stop(&host, 999); /* Separate header read STOP. */
    assert(host_protocol_read(&host) == 1 && host_protocol_read(&host) == 0);
    uint8_t payload[35];
    memcpy(payload, host.response + 2, sizeof(payload));
    assert(!strcmp((const char *)payload + 2, HOST_FIRMWARE_VERSION));
    assert(!strcmp((const char *)payload + 18, HOST_HARDWARE_REVISION));
    for (unsigned i = 2 + sizeof(HOST_FIRMWARE_VERSION); i < 18; ++i) assert(!payload[i]);
    for (unsigned i = 18 + sizeof(HOST_HARDWARE_REVISION); i < 34; ++i) assert(!payload[i]);
    assert(payload[34] == 8);
    for (unsigned i = 2; i < 35; ++i) assert(host_protocol_read(&host) == payload[i]);
    host_protocol_stop(&host, 999);
    for (unsigned i = 0; i < 300; ++i) assert(host_protocol_read(&host) == 0xff);
    request(info, sizeof(info), HOST_OK); /* Abandon previous response. */
    assert(host_protocol_read(&host) == 0);
    request(info, sizeof(info), HOST_OK);
    assert(host.response_cursor == 0 && host_protocol_read(&host) == 0);

    status(0xff);
    host_log_event_t event = request(config, sizeof(config), HOST_OK);
    assert(event.command == 2 && event.target == 3 && event.length == 15);
    assert(!memcmp(event.payload, config + 3, 15));
    assert(host_get_i16(event.payload + 11) == -15);
    for (unsigned i = 0; i < 8; ++i) {
        uint8_t enabled[] = {3, i, 1, 1};
        request(enabled, sizeof(enabled), HOST_OK);
        status(0xff); /* Stub ON never changes real status or applies config. */
        enabled[3] = 0;
        request(enabled, sizeof(enabled), HOST_OK);
    }
    host_protocol_init(&host, 0xf7);
    status(0xf7);
    request(config, sizeof(config), HOST_OK); /* Valid stub even with boot fault. */
    status(0xf7);

    const uint8_t bad_target[][4] = {{1,0,0}, {4,7,0}, {3,8,1,1}, {3,255,1,1}};
    for (unsigned i = 0; i < 4; ++i) request(bad_target[i], i < 2 ? 3 : 4, HOST_INVALID_TARGET);
    for (unsigned n = 1; n < sizeof(config); ++n) request(config, n, HOST_INVALID_ARGUMENT);
    uint8_t extra[19];
    memcpy(extra, config, sizeof(config)); extra[18] = 0;
    request(extra, sizeof(extra), HOST_INVALID_ARGUMENT);
    const unsigned fields[] = {3,7,8,9,10,11,12,13,16,17};
    for (unsigned i = 0; i < sizeof(fields)/sizeof(fields[0]); ++i) {
        uint8_t invalid[sizeof(config)]; memcpy(invalid, config, sizeof(config));
        if (fields[i] == 3) memset(invalid + 3, 0, 4);
        else invalid[fields[i]] = 2;
        request(invalid, sizeof(invalid), HOST_INVALID_ARGUMENT);
    }
    const uint8_t enabled_invalid[] = {3,3,1,2};
    request(enabled_invalid, sizeof(enabled_invalid), HOST_INVALID_ARGUMENT);
    const uint8_t unknown[] = {0x80,0xff,0};
    request(unknown, sizeof(unknown), HOST_INVALID_COMMAND);
    for (uint8_t cmd = 5; cmd <= 11; ++cmd) {
        uint8_t r[12] = {cmd, cmd >= 9 ? 0xff : 3, 0};
        if (cmd == 5) { r[2] = 9; r[3] = 1; }
        if (cmd == 7) r[2] = 1;
        if (cmd == 8) r[2] = 2;
        request(r, 3 + r[2], HOST_INVALID_COMMAND); /* Reset never ACKs. */
    }
    uint8_t max_frame[258] = {5,3,255};
    max_frame[3] = 31;
    request(max_frame, sizeof(max_frame), HOST_INVALID_ARGUMENT);
    max_frame[2] = 249;
    request(max_frame, 252, HOST_INVALID_COMMAND); /* Valid max RDS request. */
    for (unsigned i = 0; i < 1000; ++i) host_protocol_receive(&host, 0, i == 0);
    host_protocol_stop(&host, 0);
    assert(host.response[0] == HOST_INVALID_ARGUMENT);
    assert(host_protocol_pop_log(&host, &event));
    request(info, sizeof(info), HOST_OK); /* Recovery after oversized write. */

    receive(config, sizeof(config));
    host_protocol_restart(&host);
    assert(host_protocol_read(&host) == HOST_INVALID_ARGUMENT);
    assert(host_protocol_pop_log(&host, &event) && event.malformed);
    host_protocol_stop(&host, 0);
    request(info, sizeof(info), HOST_OK);
    host_protocol_init(&host, 0xff);
    for (unsigned i = 0; i <= HOST_LOG_CAPACITY; ++i) {
        receive(config, sizeof(config)); host_protocol_stop(&host, 0);
    }
    assert(host.response[0] == HOST_BUSY && host.log_busy_count == 1);
    while (host_protocol_pop_log(&host, &event)) assert(event.status == HOST_OK);
    assert(host.response[0] == HOST_BUSY); /* Draining logs does not execute BUSY. */
    status(0xff);
    puts("Host V1: framing, INFO/STATUS, split reads, validation, stub ACKs, overload PASS");
}
