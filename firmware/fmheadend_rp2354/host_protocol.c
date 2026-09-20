#include "host_protocol.h"
#include <string.h>

enum { GET_INFO = 1, SET_TX_CONFIG, SET_TX_ENABLED, GET_STATUS, RDS_PUSH,
       RDS_CLEAR, QN_READ_REGISTER, QN_WRITE_REGISTER, RESET_ERROR_COUNTERS,
       RP_SOFT_RESET, GET_RDS_FIFO_FREE };

_Static_assert(sizeof(HOST_FIRMWARE_VERSION) <= 16, "firmware version too long");
_Static_assert(sizeof(HOST_HARDWARE_REVISION) <= 16, "hardware revision too long");

uint16_t host_get_u16(const uint8_t *p) { return (uint16_t)p[0] | (uint16_t)p[1] << 8; }
uint32_t host_get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
           (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
int host_get_i16(const uint8_t *p)
{
    unsigned n = host_get_u16(p);
    return n >= 0x8000u ? (int)n - 65536 : (int)n;
}
static void put_u32(uint8_t *p, uint32_t n)
{
    for (unsigned i = 0; i < 4; ++i) p[i] = n >> (8 * i);
}
static void reply(host_protocol_t *host, uint8_t status, uint8_t length)
{
    host->response[0] = status;
    host->response[1] = length;
    host->response_size = 2u + length;
    host->response_cursor = 0;
}

void host_protocol_init(host_protocol_t *host, uint8_t ready_mask)
{
    memset(host, 0, sizeof(*host));
    host->ready_mask = ready_mask;
    reply(host, HOST_BUSY, 0); /* A read before any request is never fake OK. */
}

void host_protocol_receive(host_protocol_t *host, uint8_t byte, bool first)
{
    if (first && host->receiving) host->malformed = true; /* Missing STOP. */
    host->receiving = true;
    if (host->request_size < sizeof(host->request)) {
        host->request[host->request_size++] = byte;
    } else host->malformed = true; /* Drain/discard excess bytes, bounded memory. */
}

void host_protocol_restart(host_protocol_t *host)
{
    if (host->receiving) host->malformed = true;
}
void host_protocol_overflow(host_protocol_t *host)
{
    host->receiving = host->malformed = true;
}

static uint8_t validate(const host_protocol_t *host)
{
    const uint8_t *r = host->request;
    if (host->malformed || host->request_size < 3 ||
        host->request_size != 3u + r[2]) return HOST_INVALID_ARGUMENT;
    const uint8_t command = r[0], target = r[1], length = r[2];
    const uint8_t *p = r + 3;
    if (command < GET_INFO || command > GET_RDS_FIFO_FREE) return HOST_INVALID_COMMAND;
    const bool global = command == GET_INFO || command == GET_STATUS ||
                        command >= RESET_ERROR_COUNTERS;
    if (global ? target != 0xff : target > 7) return HOST_INVALID_TARGET;
    unsigned expected = 0;
    switch (command) {
    case SET_TX_CONFIG: expected = 15; break;
    case SET_TX_ENABLED: case QN_READ_REGISTER: expected = 1; break;
    case QN_WRITE_REGISTER: expected = 2; break;
    case RDS_PUSH:
        if (!length || p[0] < 1 || p[0] > 31) return HOST_INVALID_ARGUMENT;
        expected = 1u + 8u * p[0];
        break;
    default: break;
    }
    if (length != expected) return HOST_INVALID_ARGUMENT;
    if (command == SET_TX_ENABLED && p[0] > 1) return HOST_INVALID_ARGUMENT;
    if (command == SET_TX_CONFIG &&
        (!host_get_u32(p) || host_get_u16(p + 4) > 225 ||
         host_get_u16(p + 4) % 15 || p[6] > 1 ||
         (p[7] != 50 && p[7] != 75) || p[8] > 1 || p[9] > 1 ||
         p[10] > 1 || p[13] > 1 || p[14] > 1)) return HOST_INVALID_ARGUMENT;
    return HOST_OK;
}

static void status_payload(host_protocol_t *host, uint32_t uptime_ms)
{
    uint8_t *p = host->response + 2;
    memset(p, 0, 130);
    p[0] = host->ready_mask == 0xff ? 0 : 1;
    put_u32(p + 1, uptime_ms);
    p[9] = host->ready_mask == 0xff ? HOST_OK : HOST_QN_ERROR;
    for (unsigned i = 0; i < 8; ++i) {
        uint8_t *channel = p + 10 + 15 * i;
        /* ALL TXs remain OFF, including failed QNs held at CEN LOW.
         * No applied attenuation, saturation measurement or RDS FIFO exists.
         * Zero counters mean no detected events, not implemented monitoring.
         */
        channel[2] = 0x80; /* i16 -32768: attenuation unavailable. */
        channel[13] = host->ready_mask & (1u << i) ? HOST_OK : HOST_QN_ERROR;
        channel[14] = 255;
    }
}

void host_protocol_stop(host_protocol_t *host, uint32_t uptime_ms)
{
    if (!host->receiving) return; /* Header/payload read STOP preserves cursor. */
    uint8_t status = validate(host), length = 0;
    const uint8_t command = host->request_size ? host->request[0] : 0;
    if (host->log_count == HOST_LOG_CAPACITY) {
        /* No deferred command execution after BUSY; report overload later. */
        status = HOST_BUSY;
        if (host->log_busy_count != UINT32_MAX) ++host->log_busy_count;
    } else {
        if (status == HOST_OK) {
            switch (command) {
            case GET_INFO:
                length = 35;
                memset(host->response + 2, 0, length);
                host->response[2] = 1; /* protocol_version u16 LE */
                memcpy(host->response + 4, HOST_FIRMWARE_VERSION, sizeof(HOST_FIRMWARE_VERSION));
                memcpy(host->response + 20, HOST_HARDWARE_REVISION, sizeof(HOST_HARDWARE_REVISION));
                host->response[36] = 8;
                break;
            case GET_STATUS:
                length = 130;
                status_payload(host, uptime_ms);
                break;
            case SET_TX_CONFIG: case SET_TX_ENABLED:
                /* M8.2 deliberate ACK stubs: no stored config, QN IO or RF. */
                break;
            default:
                status = HOST_INVALID_COMMAND; /* Includes RP_SOFT_RESET. */
                break;
            }
        }
        host_log_event_t *log = &host->logs[host->log_head];
        memset(log, 0, sizeof(*log));
        log->command = command;
        log->target = host->request_size > 1 ? host->request[1] : 0xff;
        log->length = host->request_size > 2 ? host->request[2] : 0;
        log->malformed = host->malformed || host->request_size < 3 ||
                         host->request_size != 3u + log->length;
        log->status = status;
        log->response_length = length;
        if (status == HOST_OK && (command == SET_TX_CONFIG || command == SET_TX_ENABLED)) {
            memcpy(log->payload, host->request + 3, log->length);
        }
        host->log_head = (host->log_head + 1) % HOST_LOG_CAPACITY;
        ++host->log_count;
    }
    reply(host, status, length);
    host->request_size = 0;
    host->receiving = host->malformed = false;
}

uint8_t host_protocol_read(host_protocol_t *host)
{
    if (host->receiving) {
        /* A write followed by read without STOP cannot execute anything. */
        host->malformed = true;
        host_protocol_stop(host, 0);
    }
    if (host->response_cursor >= host->response_size) return 0xff;
    return host->response[host->response_cursor++];
}
void host_protocol_unread(host_protocol_t *host, unsigned count)
{
    host->response_cursor -= count < host->response_cursor ? count : host->response_cursor;
}
bool host_protocol_pop_log(host_protocol_t *host, host_log_event_t *event)
{
    if (!host->log_count) return false;
    *event = host->logs[host->log_tail];
    host->log_tail = (host->log_tail + 1) % HOST_LOG_CAPACITY;
    --host->log_count;
    return true;
}
const char *host_command_name(uint8_t command)
{
    static const char *const names[] = {"UNKNOWN", "GET_INFO", "SET_TX_CONFIG",
        "SET_TX_ENABLED", "GET_STATUS", "RDS_PUSH", "RDS_CLEAR", "QN_READ_REGISTER",
        "QN_WRITE_REGISTER", "RESET_ERROR_COUNTERS", "RP_SOFT_RESET", "GET_RDS_FIFO_FREE"};
    return command <= GET_RDS_FIFO_FREE ? names[command] : names[0];
}
const char *host_status_name(uint8_t status)
{
    static const char *const names[] = {"OK", "INVALID_COMMAND", "INVALID_TARGET",
        "INVALID_ARGUMENT", "BUSY", "QN_ERROR", "INTERNAL_ERROR"};
    return status <= HOST_INTERNAL_ERROR ? names[status] : "UNKNOWN";
}
