#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "host_test_frame.h"
#include "fake_qn.h"

static host_protocol_t host;
static const uint8_t config[] = {2, 3, 15, 0x34, 0x7f, 1, 0, 60, 0,
                               1, 50, 0, 0, 1, 0xf1, 0xff, 1, 1};

static void work(void)
{
    host_work_t w;
    if (host_protocol_take_work(&host,&w)) {
        host_protocol_run_work(&host,&w,0x78563412);
        host_protocol_complete_work(&host,&w);
    }
}
static void receive(const uint8_t *r, size_t n)
{
    test_receive(&host, r, n);
}
static host_log_event_t request(const uint8_t *r, size_t n, uint8_t expected)
{
    receive(r, n);
    host_protocol_stop(&host, 0x78563412);
    work();
    test_response_crc(&host);
    assert(host.response[0] == expected);
    if (expected) assert(host.response_size == 4 && host.response[1] == 0);
    host_log_event_t event = {0};
    if (host_protocol_pop_log(&host, &event)) assert(event.status == expected);
    else assert(expected == HOST_OK && (r[0] == 5 || r[0] == 6 || r[0] == 11));
    return event;
}
static void status(uint8_t ready_mask)
{
    const uint8_t req[] = {4, 0xff, 0};
    request(req, sizeof(req), HOST_OK);
    assert(host.response_size == 134 && host.response[1] == 130);
    const uint8_t *p = host.response + 2;
    assert(p[0] == (ready_mask != 0xff));
    assert(host_get_u32(p + 1) == 0x78563412);
    assert(host_get_u32(p + 5) == 0 && p[9] == (ready_mask == 0xff ? 0 : 5));
    for (unsigned i = 0; i < 8; ++i) {
        const uint8_t *c = p + 10 + i * 15;
        assert(c[0] == (ready_mask & (1u << i) ? 0 : 2) && host_get_i16(c + 1) == -32768);
        assert(host_get_u32(c + 3) == 0 && host_get_u32(c + 7) == 0);
        assert(host_get_u16(c + 11) == RDS_FIFO_CAPACITY && c[14] == 255);
        assert(c[13] == (ready_mask & (1u << i) ? 0 : 5));
    }
}
static void crc_rejection(void)
{
    assert(host_crc16((const uint8_t *)"123456789", 9) == 0x29b1);
    const uint8_t info[] = {1,255,0};
    assert(host_crc16(info,sizeof(info)) == 0xf853);
    uint8_t max[254] = {5,7,249,31};
    for(unsigned i=0;i<248;++i) max[4+i]=(uint8_t)i;
    uint16_t crc=host_crc16(max,252);
    assert(crc==0x75f7); /* Shared C/Python wire vector. */
    max[252]=(uint8_t)crc; max[253]=(uint8_t)(crc>>8);
    /* Every single-bit error, including command/target/length and CRC, must
     * reject the maximum 31-group PUSH without allocating even one group. */
    for(unsigned bit=0;bit<sizeof(max)*8;++bit) {
        host_protocol_init(&host,255);
        max[bit/8]^=1u<<(bit%8);
        for(unsigned i=0;i<sizeof(max);++i) host_protocol_receive(&host,max[i],i==0);
        max[bit/8]^=1u<<(bit%8);
        unsigned before=fake_calls;
        host_protocol_stop(&host,0);work();
        assert(host.response[0]==HOST_INVALID_ARGUMENT && !host.pending);
        test_response_crc(&host);
        assert(fake_calls==before);
        for(unsigned i=0;i<8;++i) assert(rds_fifo_free(&host.rds[i])==64);
    }
    /* Missing/short/extra CRC bytes fail; next complete valid frame recovers. */
    for(unsigned n=252;n<254;++n) {
        host_protocol_init(&host,255);
        for(unsigned i=0;i<n;++i) host_protocol_receive(&host,max[i],i==0);
        host_protocol_stop(&host,0);
        assert(host.response[0]==HOST_INVALID_ARGUMENT);
        test_response_crc(&host);
    }
    host_protocol_init(&host,255);
    for(unsigned i=0;i<sizeof(max);++i) host_protocol_receive(&host,max[i],i==0);
    host_protocol_receive(&host,0,false);
    host_protocol_stop(&host,0);
    assert(host.response[0]==HOST_INVALID_ARGUMENT);
    test_response_crc(&host);
    host_log_event_t discarded;
    while(host_protocol_pop_log(&host,&discarded)) {}
    request(max,252,HOST_OK);
    assert(rds_fifo_free(&host.rds[7])==33);
    assert(host.response_size==4 && host.response[2]==0x0f && host.response[3]==0x1d);
    const uint8_t clear[]={6,7,0};
    receive(clear,sizeof(clear)); host.request[3]^=1;
    host_protocol_stop(&host,0);
    assert(host.response[0]==HOST_INVALID_ARGUMENT && rds_fifo_free(&host.rds[7])==33);
    /* A corrupt side-effecting QN command never reaches the foreground. */
    host_protocol_init(&host,255);
    receive(config,sizeof(config));
    host.request[host.request_size-1]^=0x80;
    unsigned before=fake_calls;
    host_protocol_stop(&host,0);work();
    assert(host.response[0]==HOST_INVALID_ARGUMENT && fake_calls==before);
    test_response_crc(&host);
}
int main(void)
{
    fake_qn_reset();
    crc_rejection();
    host_protocol_init(&host, 0xff);
    assert(host_protocol_read(&host) == HOST_BUSY && host_protocol_read(&host) == 0);
    const uint8_t info[] = {1, 0xff, 0};
    request(info, sizeof(info), HOST_OK);
    assert(host.response_size == 51);
    assert(host_protocol_read(&host) == 0 && host_protocol_read(&host) == 47);
    host_protocol_stop(&host, 999); /* Separate header read STOP. */
    assert(host_protocol_read(&host) == 1 && host_protocol_read(&host) == 0);
    uint8_t payload[47];
    memcpy(payload, host.response + 2, sizeof(payload));
    assert(!strcmp((const char *)payload + 2, HOST_FIRMWARE_VERSION));
    assert(!strcmp((const char *)payload + 18, HOST_HARDWARE_TARGET));
    assert(!strcmp((const char *)payload + 18, "rp2354-qn8x-v1"));
    assert(host_get_u32(payload + 35) == 76000);
    assert(host_get_u32(payload + 39) == 108000);
    assert(host_get_u32(payload + 43) == 50);
    for (unsigned i = 2 + sizeof(HOST_FIRMWARE_VERSION); i < 18; ++i) assert(!payload[i]);
    for (unsigned i = 18 + sizeof(HOST_HARDWARE_TARGET); i < 34; ++i) assert(!payload[i]);
    assert(payload[34] == 8);
    for (unsigned i = 2; i < 47; ++i) assert(host_protocol_read(&host) == payload[i]);
    assert(host_protocol_read(&host) == host.response[49]);
    assert(host_protocol_read(&host) == host.response[50]);
    host_protocol_stop(&host, 999);
    for (unsigned i = 0; i < 300; ++i) assert(host_protocol_read(&host) == 0xff);
    request(info, sizeof(info), HOST_OK); /* Abandon previous response. */
    assert(host_protocol_read(&host) == 0);
    request(info, sizeof(info), HOST_OK);
    assert(host.response_cursor == 0 && host_protocol_read(&host) == 0);

    status(0xff);
    const uint8_t diagnostics[] = {0x0c,255,0};
    request(diagnostics,sizeof(diagnostics),HOST_OK);
    assert(host.response_size==36 && host.response[1]==32);
    const uint8_t diagnostics_target[] = {0x0c,0,0};
    request(diagnostics_target,sizeof(diagnostics_target),HOST_INVALID_TARGET);
    const uint8_t diagnostics_payload[] = {0x0c,255,1,0};
    request(diagnostics_payload,sizeof(diagnostics_payload),HOST_INVALID_ARGUMENT);
    assert(host_response_wait_ms(0x0c)==2);
    assert(!strcmp(host_command_name(0x0c),"GET_SPI_DIAGNOSTICS"));
    host_log_event_t event = request(config, sizeof(config), HOST_OK);
    assert(event.command == 2 && event.target == 3 && event.length == 15);
    assert(!memcmp(event.payload, config + 3, 15));
    assert(host_get_i16(event.payload + 11) == -15);
    const uint8_t on[] = {3,3,1,1}, off[] = {3,3,1,0};
    request(on,sizeof(on),HOST_OK);
    assert(host.qn.channel[3].enabled);
    request(off,sizeof(off),HOST_OK);
    assert(!host.qn.channel[3].enabled);
    fake_qn_reset();
    host_protocol_init(&host, 0xf7);
    status(0xf7);
    request(config, sizeof(config), HOST_QN_ERROR);
    status(0xf7);
    fake_qn_reset(); host_protocol_init(&host,0xff);

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
        else invalid[fields[i]] = fields[i] == 11 ? 3 : 2;
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
        request(r, 3 + r[2], cmd==9 || cmd==10 ? HOST_INVALID_COMMAND : HOST_OK); /* Reset never ACKs. */
    }
    uint8_t max_frame[258] = {5,3,255};
    max_frame[3] = 31;
    request(max_frame, sizeof(max_frame), HOST_INVALID_ARGUMENT);
    max_frame[2] = 249;
    request(max_frame, 252, HOST_OK); /* Valid max RDS request. */
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
        receive(config, sizeof(config)); host_protocol_stop(&host, 0); work();
    }
    assert(host.response[0] == HOST_BUSY && host.log_busy_count == 1);
    while (host_protocol_pop_log(&host, &event)) assert(event.status == HOST_OK);
    assert(host.response[0] == HOST_BUSY); /* Draining logs does not execute BUSY. */
    fake_qn_reset(); host_protocol_init(&host,0xff);
    status(0xff);
    /* Early read cancels pending work: BUSY can never execute later. */
    receive(config,sizeof(config)); host_protocol_stop(&host,0);
    unsigned before=fake_calls;
    assert(host_protocol_read(&host)==HOST_BUSY);
    work(); assert(fake_calls==before);
    while(host_protocol_pop_log(&host,&event)) {}
    /* Once execution starts an early read is unknown outcome, NOT BUSY. */
    receive(config,sizeof(config));host_protocol_stop(&host,0);
    host_work_t pending;assert(host_protocol_take_work(&host,&pending));
    assert(host_protocol_read(&host)==HOST_INTERNAL_ERROR);
    host_protocol_run_work(&host,&pending,0);
    host_protocol_complete_work(&host,&pending);
    assert(host.response[0]==HOST_INTERNAL_ERROR); /* No late OK replacement. */
    while(host_protocol_pop_log(&host,&event)) {}
    /* A new complete request cannot interleave a QN operation or steal reply. */
    receive(config,sizeof(config));host_protocol_stop(&host,0);
    assert(host_protocol_take_work(&host,&pending));
    receive(info,sizeof(info));host_protocol_stop(&host,0);
    assert(host_protocol_read(&host)==HOST_BUSY);
    host_protocol_run_work(&host,&pending,0);host_protocol_complete_work(&host,&pending);
    assert(host.response[0]==HOST_BUSY);
    while(host_protocol_pop_log(&host,&event)) {}
    request(info,sizeof(info),HOST_OK);
    assert(host_response_wait_ms(1)==2 && host_response_wait_ms(2)==1000);
    assert(host_response_wait_ms(3)==150 && host_response_wait_ms(4)==850);
    assert(host_response_wait_ms(7)==100 && host_response_wait_ms(8)==100);
    puts("Host V1: framing, INFO/STATUS, split reads, validation, foreground execution, early reads, overload PASS");
}
