#ifndef FMHEADEND_HOST_PROTOCOL_H
#define FMHEADEND_HOST_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "qn_control.h"
#include "rds_fifo.h"
#include "rds_tx.h"

#define HOST_FIRMWARE_VERSION "0.8.6b"
#define HOST_HARDWARE_REVISION "PCB1.0"
#define HOST_REQUEST_MAX 260u
#define HOST_RESPONSE_MAX 134u
#define HOST_LOG_CAPACITY 16u

enum host_status {
    HOST_OK = 0, HOST_INVALID_COMMAND = 1, HOST_INVALID_TARGET = 2,
    HOST_INVALID_ARGUMENT = 3, HOST_BUSY = 4, HOST_QN_ERROR = 5,
    HOST_INTERNAL_ERROR = 6,
};

typedef struct {
    uint8_t command, target, length, status, response_length;
    uint8_t payload[15]; /* SET fields or concise command metadata, never a binary dump. */
    bool malformed;
} host_log_event_t;

typedef struct {
    host_log_event_t event;
    uint32_t generation;
    uint8_t result[130];
    uint8_t update_payload[255];
} host_work_t;

/* IRQ owns framing; foreground owns QN state. Work/log handoffs use short IRQ
 * masks. No QN execution, UART, allocation or waiting inside the IRQ.
 */
typedef struct {
    uint8_t ready_mask;
    uint8_t request[HOST_REQUEST_MAX];
    size_t request_size;
    bool receiving, malformed;
    uint8_t response[HOST_RESPONSE_MAX];
    size_t response_size, response_cursor;
    host_log_event_t logs[HOST_LOG_CAPACITY];
    unsigned log_head, log_tail, log_count;
    uint32_t log_busy_count;
    qn_control_t qn;
    rds_fifo_t rds[8]; /* Core0 only; see rds_fifo.h ownership contract. */
    rds_tx_t rds_tx; /* Foreground only; IRQ only changes the FIFO/epoch. */
    bool pending, running;
    bool update_reply_deferred;
    uint32_t generation;
    host_work_t work;
} host_protocol_t;

/* Take/complete under a short IRQ mask; run WITHOUT masking interrupts. */
bool host_protocol_take_work(host_protocol_t *host, host_work_t *work);
void host_protocol_run_work(host_protocol_t *host, host_work_t *work, uint32_t uptime);
void host_protocol_complete_work(host_protocol_t *host, const host_work_t *work);
unsigned host_response_wait_ms(uint8_t command);

void host_protocol_init(host_protocol_t *host, uint8_t ready_mask);
void host_protocol_receive(host_protocol_t *host, uint8_t byte, bool first);
void host_protocol_restart(host_protocol_t *host);
void host_protocol_overflow(host_protocol_t *host);
/* STOP commits a complete write; read STOP never resets the response cursor. */
void host_protocol_stop(host_protocol_t *host, uint32_t uptime_ms);
uint8_t host_protocol_read(host_protocol_t *host);
/* Undo a byte queued by hardware but flushed before transmission. */
void host_protocol_unread(host_protocol_t *host, unsigned count);
bool host_protocol_pop_log(host_protocol_t *host, host_log_event_t *event);
const char *host_command_name(uint8_t command);
const char *host_status_name(uint8_t status);
uint16_t host_crc16(const uint8_t *data, size_t size);
uint16_t host_get_u16(const uint8_t *p);
uint32_t host_get_u32(const uint8_t *p);
int host_get_i16(const uint8_t *p);

#endif
