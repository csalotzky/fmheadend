#include "host_protocol.h"
#include "firmware_config.h"
#include <string.h>
#ifdef FMHEADEND_FLASH_UPDATE
#include "flash_update.h"
#endif

enum { GET_INFO = 1, SET_TX_CONFIG, SET_TX_ENABLED, GET_STATUS, RDS_PUSH,
       RDS_CLEAR, QN_READ_REGISTER, QN_WRITE_REGISTER, RESET_ERROR_COUNTERS,
       RP_SOFT_RESET, GET_RDS_FIFO_FREE };

_Static_assert(sizeof(HOST_FIRMWARE_VERSION) <= 16, "firmware version too long");
_Static_assert(sizeof(HOST_HARDWARE_TARGET) <= 16, "hardware target too long");

uint16_t host_crc16(const uint8_t *data, size_t size)
{
    uint16_t crc = 0xffff;
    for (size_t i = 0; i < size; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (uint16_t)((crc << 1) ^ ((crc & 0x8000) ? 0x1021 : 0));
    }
    return crc;
}

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
static void reply(host_protocol_t *host, uint8_t status, uint8_t length)
{
    host->response[0] = status;
    host->response[1] = length;
    const uint16_t crc = host_crc16(host->response, 2u + length);
    host->response[2u + length] = (uint8_t)crc;
    host->response[3u + length] = (uint8_t)(crc >> 8);
    host->response_size = 4u + length;
    host->response_cursor = 0;
}

void host_protocol_init(host_protocol_t *host, uint8_t ready_mask)
{
    memset(host, 0, sizeof(*host));
    host->ready_mask = ready_mask;
    qn_control_init(&host->qn, ready_mask);
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

static bool valid_frame(const host_protocol_t *host)
{
    return !host->malformed && host->request_size >= 5 &&
           host->request_size == 5u + host->request[2] &&
           host_crc16(host->request, host->request_size - 2) ==
               host_get_u16(host->request + host->request_size - 2);
}

static uint8_t validate(const host_protocol_t *host)
{
    const uint8_t *r = host->request;
    const uint8_t command = r[0], target = r[1], length = r[2];
    const uint8_t *p = r + 3;
#ifdef FMHEADEND_FLASH_UPDATE
    if(command==FU_COMMAND) {
        if(target!=0xff) return HOST_INVALID_TARGET;
        return length ? HOST_OK : HOST_INVALID_ARGUMENT;
    }
#endif
    if (command < GET_INFO || command > GET_RDS_FIFO_FREE) return HOST_INVALID_COMMAND;
    const bool global = command == GET_INFO || command == GET_STATUS ||
                        command >= RESET_ERROR_COUNTERS;
    if (global ? target != 0xff : target >= TX_COUNT) return HOST_INVALID_TARGET;
    unsigned expected = 0;
    switch (command) {
    case SET_TX_CONFIG: expected = 15; break;
    case SET_TX_ENABLED: case QN_READ_REGISTER: expected = 1; break;
    case QN_WRITE_REGISTER: expected = 2; break;
    case RDS_PUSH:
        if (!length || p[0] < 1 || p[0] > RDS_PUSH_MAX_GROUPS) return HOST_INVALID_ARGUMENT;
        expected = 1u + RDS_GROUP_BYTES * p[0];
        break;
    default: break;
    }
    if (length != expected) return HOST_INVALID_ARGUMENT;
    if (command == SET_TX_ENABLED && p[0] > 1) return HOST_INVALID_ARGUMENT;
    if (command == SET_TX_CONFIG &&
        (!host_get_u32(p) || host_get_u16(p + 4) > 225 ||
         host_get_u16(p + 4) % 15 || p[6] > 1 ||
         (p[7] != 50 && p[7] != 75) || p[8] > 2 || p[9] > 1 ||
         p[10] > 1 || p[13] > 1 || p[14] > 1)) return HOST_INVALID_ARGUMENT;
    return HOST_OK;
}

unsigned host_response_wait_ms(uint8_t command)
{
    switch (command) {
#ifdef FMHEADEND_FLASH_UPDATE
    case FU_COMMAND: return 2;
#endif
    case SET_TX_CONFIG: return 1000;
    case GET_STATUS: return 850;
    case SET_TX_ENABLED: return 150;
    case QN_READ_REGISTER: case QN_WRITE_REGISTER: return 100;
    default: return 2;
    }
}
static void log_event(host_protocol_t *host, const host_log_event_t *event)
{
    if (host->log_count == HOST_LOG_CAPACITY) {
        if (host->log_busy_count != UINT32_MAX) ++host->log_busy_count;
        return;
    }
    host->logs[host->log_head] = *event;
    host->log_head = (host->log_head + 1) % HOST_LOG_CAPACITY;
    ++host->log_count;
}
static void cancel_pending(host_protocol_t *host)
{
    if (!host->pending) return;
    host->pending = false;
    host->work.event.status = HOST_BUSY;
    log_event(host, &host->work.event);
}
void host_protocol_stop(host_protocol_t *host, uint32_t uptime_ms)
{
    (void)uptime_ms;
    if (!host->receiving) return;
    /* Validate integrity before dispatch or any command side effect. */
    const bool frame_ok = valid_frame(host);
    host->update_reply_deferred=false;
    cancel_pending(host); /* New complete request abandons unstarted work. */
    ++host->generation;
    host_log_event_t event = {0};
    bool quiet = false;
    event.status = frame_ok ? validate(host) : HOST_INVALID_ARGUMENT;
    event.command = host->request_size ? host->request[0] : 0;
    event.target = host->request_size > 1 ? host->request[1] : 0xff;
    event.length = host->request_size > 2 ? host->request[2] : 0;
    event.malformed = !frame_ok;
    if (!event.malformed && event.length <= sizeof(event.payload))
        memcpy(event.payload, host->request + 3, event.length);
    if (event.status == HOST_OK && (host->running || host->log_count == HOST_LOG_CAPACITY)) {
        event.status = HOST_BUSY; /* This NEW request will not execute. */
    } else if (event.status == HOST_OK) {
        switch (event.command) {
#ifdef FMHEADEND_FLASH_UPDATE
        case FU_COMMAND:
            host->work=(host_work_t){.event=event,.generation=host->generation};
            memcpy(host->work.update_payload,host->request+3,event.length);
            host->pending=true;
            break;
#endif
        case GET_INFO:
            event.response_length = 47;
            memset(host->response + 2, 0, 47);
            host->response[2] = 1;
            memcpy(host->response + 4, HOST_FIRMWARE_VERSION, sizeof(HOST_FIRMWARE_VERSION));
            memcpy(host->response + 20, HOST_HARDWARE_TARGET, sizeof(HOST_HARDWARE_TARGET));
            host->response[36] = TX_COUNT;
            const uint32_t frequencies[] = {76000, 108000, 50};
            for (unsigned field=0; field<3; ++field)
                for (unsigned byte=0; byte<4; ++byte)
                    host->response[37+4*field+byte] = (uint8_t)(frequencies[field] >> (8*byte));
            break;
        case RDS_PUSH: {
            rds_fifo_t *fifo = &host->rds[event.target];
            const unsigned before = rds_fifo_free(fifo);
            /* Use the complete request, not the small UART metadata copy. */
            event.payload[0] = host->request[3];
            if (!rds_fifo_push(fifo, host->request + 4, event.payload[0]))
                event.status = HOST_BUSY;
            event.payload[1] = rds_fifo_free(fifo);
            /* Log empty -> nonempty and full transitions, and every error. */
            quiet = event.status == HOST_OK && before != RDS_FIFO_CAPACITY &&
                    event.payload[1] != 0;
            break;
        }
        case RDS_CLEAR:
            quiet = rds_fifo_free(&host->rds[event.target]) == RDS_FIFO_CAPACITY;
            rds_fifo_clear(&host->rds[event.target]);
            break;
        case GET_RDS_FIFO_FREE:
            event.response_length = 16;
            for (unsigned i = 0; i < 8; ++i) {
                const uint16_t free = (i<TX_COUNT ? rds_fifo_free(&host->rds[i]) : 0);
                host->response[2 + 2*i] = free;
                host->response[3 + 2*i] = free >> 8;
            }
            quiet = true; /* Polling is not a state transition. */
            break;
        case GET_STATUS: case SET_TX_CONFIG: case SET_TX_ENABLED:
        case QN_READ_REGISTER: case QN_WRITE_REGISTER:
            host->work = (host_work_t){.event=event, .generation=host->generation};
            host->pending = true;
            break;
        default: event.status = HOST_INVALID_COMMAND; break;
        }
    }
    if (!host->pending && !quiet) log_event(host, &event);
    /* Pending is not an OK response: an early read cancels it before execution,
     * or reports INTERNAL_ERROR (unknown outcome) if execution already began.
     */
    reply(host, host->pending ? HOST_INTERNAL_ERROR : event.status, event.response_length);
#ifdef FMHEADEND_FLASH_UPDATE
    if(host->pending && event.command==FU_COMMAND) reply(host,HOST_BUSY,0);
#endif
    host->request_size = 0;
    host->receiving = host->malformed = false;
}
bool host_protocol_take_work(host_protocol_t *host, host_work_t *work)
{
    if (!host->pending || host->running) return false;
    *work=host->work;
    host->pending=false; host->running=true;
    return true;
}
void host_protocol_run_work(host_protocol_t *host, host_work_t *work, uint32_t uptime)
{
    host_log_event_t *e=&work->event;
#ifdef FMHEADEND_FLASH_UPDATE
    if(e->command==FU_COMMAND) {
        e->status=flash_update_execute(work->update_payload,e->length,work->result);
        e->response_length=e->status==HOST_OK ? (work->update_payload[0]==FU_WRITE ? FU_WRITE_REPLY_SIZE : FU_REPLY_SIZE) : 0;
        return;
    }
    if(flash_update_maintenance() && e->command!=GET_STATUS) {
        e->status=HOST_BUSY; e->response_length=0; return;
    }
#endif
    if (e->command==GET_STATUS) {
        qn_control_status(&host->qn,work->result,uptime);
        e->response_length=130;
    } else {
        e->status=qn_control_execute(&host->qn,e->command,e->target,e->payload,work->result);
        e->response_length=e->status==HOST_OK && e->command==QN_READ_REGISTER ? 1 : 0;
        if (e->response_length) e->payload[1]=work->result[0];
    }
}
void host_protocol_complete_work(host_protocol_t *host, const host_work_t *work)
{
    host->running=false;
#ifdef FMHEADEND_FLASH_UPDATE
    /* Keep a partially read BUSY frame immutable across the two READs. The
     * completed reply replaces it only at the next response boundary. */
    if(work->event.command==FU_COMMAND && host->generation==work->generation &&
       host->response_cursor>0 && host->response_cursor<host->response_size) {
        host->work=*work; host->update_reply_deferred=true; return;
    }
#endif
    if (host->generation==work->generation) {
        memcpy(host->response+2,work->result,work->event.response_length);
        if (work->event.command == GET_STATUS && work->event.status == HOST_OK) {
            /* complete_work runs under the existing core0 IRQ mask. Snapshot
             * transport state here, after the slow QN status reads finish. */
            for (unsigned i = 0; i < 8; ++i) {
                const uint16_t free = (i<TX_COUNT ? rds_fifo_free(&host->rds[i]) : 0);
                uint8_t *channel = host->response + 2 + 10 + 15*i;
                channel[11] = free;
                channel[12] = free >> 8;
            }
        }
        reply(host,work->event.status,work->event.response_length);
    }
    log_event(host,&work->event);
}

uint8_t host_protocol_read(host_protocol_t *host)
{
    if (host->receiving) {
        /* A write followed by read without STOP cannot execute anything. */
        host->malformed = true;
        host_protocol_stop(host, 0);
    }
#ifdef FMHEADEND_FLASH_UPDATE
    if(host->update_reply_deferred && host->response_cursor>=host->response_size) {
        host->update_reply_deferred=false;
        host_protocol_complete_work(host,&host->work);
    }
    bool update_wait=(host->pending || host->running) && host->work.event.command==FU_COMMAND;
    if(update_wait) {
        if(host->response_cursor>=host->response_size) reply(host,HOST_BUSY,0);
    } else
#endif
    if (host->pending) {
        cancel_pending(host);
        ++host->generation;
        reply(host, HOST_BUSY, 0);
    } else if (host->running && host->generation == host->work.generation) {
        /* Violated the per-command wait: outcome unknown, never replay as BUSY. */
        ++host->generation;
        reply(host, HOST_INTERNAL_ERROR, 0);
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
#ifdef FMHEADEND_FLASH_UPDATE
    if(command==FU_COMMAND) return "FLASH_UPDATE";
#endif
    return command <= GET_RDS_FIFO_FREE ? names[command] : names[0];
}
const char *host_status_name(uint8_t status)
{
    static const char *const names[] = {"OK", "INVALID_COMMAND", "INVALID_TARGET",
        "INVALID_ARGUMENT", "BUSY", "QN_ERROR", "INTERNAL_ERROR"};
    return status <= HOST_INTERNAL_ERROR ? names[status] : "UNKNOWN";
}
