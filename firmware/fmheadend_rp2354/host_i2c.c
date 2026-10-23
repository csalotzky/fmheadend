#include "host_i2c.h"
#include "host_protocol.h"
#include "board.h"
#include "audio_engine.h"
#include "spi_audio.h"
#include "pico/stdlib.h"
#include "hardware/i2c.h"
#include "hardware/irq.h"
#include "hardware/sync.h"
#include "hardware/uart.h"
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#ifdef FMHEADEND_FLASH_UPDATE
#include "flash_update.h"
static volatile bool update_ack_read;
static volatile uint32_t update_ack_generation;
static volatile bool update_ack_armed;
static volatile uint8_t update_ack_command;
#endif

#if !defined(FMHEADEND_HOST_I2C_ADDRESS) || FMHEADEND_HOST_I2C_ADDRESS < 0x08 || FMHEADEND_HOST_I2C_ADDRESS > 0x77
#error "Configure an unreserved 7-bit FMHEADEND_HOST_I2C_ADDRESS"
#endif

#define HOST_I2C i2c1
static host_protocol_t host;
static bool last_tx_advanced;
/* First abort only. ISR publishes once; core0 formats the immutable copy later. */
static struct {
    uint64_t timestamp_us;
    uint32_t source, flags, raw, intr, txflr, rxflr, status;
    size_t cursor, size, copied;
    bool last_tx_advanced, receiving, pending, running;
    uint8_t work_command, work_target;
    uint8_t response[40];
} abrt_snapshot;
static atomic_bool abrt_snapshot_valid;
static bool abrt_snapshot_printed;
static char log_line[512];
static size_t log_size, log_cursor;
#if DEBUG_UART_ENABLED
static void log_append(const char *text)
{
    size_t n=strlen(text);
    if (n > sizeof(log_line)-1-log_size) n=sizeof(log_line)-1-log_size;
    memcpy(log_line+log_size,text,n); log_size+=n;
}
static void log_drain(void)
{
    for (unsigned n=0;n<16 && log_cursor<log_size && uart_is_writable(DEBUG_UART);++n)
        uart_putc_raw(DEBUG_UART,log_line[log_cursor++]);
}
static void log_abort_snapshot(void)
{
    char line[384];
    snprintf(line,sizeof(line),
        "HOST I2C ABRT SNAP: timestamp_us=%llu\r\n"
        "source=0x%08lX flags=0x%08lX raw=0x%08lX intr=0x%08lX\r\n"
        "txflr=%lu rxflr=%lu status=0x%08lX\r\n"
        "cursor=%lu size=%lu last_tx_advanced=%u receiving=%u\r\n"
        "pending=%u running=%u work_cmd=0x%02X work_target=0x%02X\r\n"
        "flush_cnt=%lu copied=%lu\r\nresponse=",
        (unsigned long long)abrt_snapshot.timestamp_us,
        (unsigned long)abrt_snapshot.source,(unsigned long)abrt_snapshot.flags,
        (unsigned long)abrt_snapshot.raw,(unsigned long)abrt_snapshot.intr,
        (unsigned long)abrt_snapshot.txflr,(unsigned long)abrt_snapshot.rxflr,
        (unsigned long)abrt_snapshot.status,(unsigned long)abrt_snapshot.cursor,
        (unsigned long)abrt_snapshot.size,abrt_snapshot.last_tx_advanced,
        abrt_snapshot.receiving,abrt_snapshot.pending,abrt_snapshot.running,
        abrt_snapshot.work_command,abrt_snapshot.work_target,
        (unsigned long)((abrt_snapshot.source >> 23) & 0x1ffu),
        (unsigned long)abrt_snapshot.copied);
    log_append(line);
    for (size_t i=0;i<abrt_snapshot.copied;++i) {
        char byte[4];
        snprintf(byte,sizeof(byte),"%02X ",abrt_snapshot.response[i]);
        log_append(byte);
    }
    log_append("\r\n");
    abrt_snapshot_printed=true;
}
#endif
bool host_i2c_led(uint64_t now_us) { return qn_control_led(&host.qn,now_us); }

/* Own the IRQ so RX FIFO is drained BEFORE committing a STOP. The SDK's slave
 * helper reports FINISH before RECEIVE when both interrupt flags are pending,
 * and combines STOP/repeated START. V1 commits writes only at STOP.
 * Work here is bounded byte movement/validation/serialization only: no UART,
 * QN operations, sleeps, allocation or foreground scheduling dependency.
 */
static void host_i2c_irq(void)
{
    i2c_hw_t *hw = i2c_get_hw(HOST_I2C);
    const uint32_t flags = hw->intr_stat;
    if (flags & I2C_IC_INTR_STAT_R_TX_ABRT_BITS) {
        const uint32_t source = hw->tx_abrt_source;
        if (!atomic_load_explicit(&abrt_snapshot_valid,memory_order_relaxed)) {
            abrt_snapshot.source=source;
            abrt_snapshot.flags=flags;
            abrt_snapshot.raw=hw->raw_intr_stat;
            abrt_snapshot.intr=hw->intr_stat;
            abrt_snapshot.txflr=hw->txflr;
            abrt_snapshot.rxflr=hw->rxflr;
            abrt_snapshot.status=hw->status;
            abrt_snapshot.timestamp_us=time_us_64();
            abrt_snapshot.cursor=host.response_cursor;
            abrt_snapshot.size=host.response_size;
            abrt_snapshot.last_tx_advanced=last_tx_advanced;
            abrt_snapshot.receiving=host.receiving;
            abrt_snapshot.pending=host.pending;
            abrt_snapshot.running=host.running;
            abrt_snapshot.work_command=(host.pending || host.running) ? host.work.event.command : 0;
            abrt_snapshot.work_target=(host.pending || host.running) ? host.work.event.target : 0;
            size_t n=host.response_size;
            if (n>sizeof(host.response)) n=sizeof(host.response);
            if (n>sizeof(abrt_snapshot.response)) n=sizeof(abrt_snapshot.response);
            abrt_snapshot.copied=n;
            memcpy(abrt_snapshot.response,host.response,n);
            atomic_store_explicit(&abrt_snapshot_valid,true,memory_order_release);
        }
        if (last_tx_advanced && (source & I2C_IC_TX_ABRT_SOURCE_TX_FLUSH_CNT_BITS)) {
            host_protocol_unread(&host, 1);
        }
        last_tx_advanced = false;
        (void)hw->clr_tx_abrt;
    }
    if (flags & I2C_IC_INTR_STAT_R_RX_OVER_BITS) {
        (void)hw->clr_rx_over;
        host_protocol_overflow(&host);
    }
    /* FIFO depth is 16. Snapshot the count to bound this IRQ even under traffic. */
    const unsigned available = i2c_get_read_available(HOST_I2C);
    for (unsigned i = 0; i < available; ++i) {
        const uint32_t word = hw->data_cmd;
        host_protocol_receive(&host, word & 0xffu,
                              (word & I2C_IC_DATA_CMD_FIRST_DATA_BYTE_BITS) != 0);
    }
    if (flags & I2C_IC_INTR_STAT_R_RESTART_DET_BITS) {
        (void)hw->clr_restart_det;
        host_protocol_restart(&host);
    }
    if (flags & I2C_IC_INTR_STAT_R_STOP_DET_BITS) {
        (void)hw->clr_stop_det;
#ifdef FMHEADEND_FLASH_UPDATE
        if(update_ack_armed && !host.receiving && host.response[0]==HOST_OK &&
           host.response_size==FU_REPLY_SIZE+4 && host.generation==update_ack_generation &&
           host.response_cursor==host.response_size && hw->txflr==0 &&
           !(flags & I2C_IC_INTR_STAT_R_TX_ABRT_BITS)) update_ack_read=true;
        if(host.receiving) { update_ack_read=false; update_ack_armed=false; }
#endif
        host_protocol_stop(&host, (uint32_t)(time_us_64() / 1000));
    }
    if (flags & I2C_IC_INTR_STAT_R_RX_DONE_BITS) {
        /* Master NACKs the last byte of EACH read, including the header.
         * Preserve the cursor so the next read starts with the payload.
         */
        (void)hw->clr_rx_done;
        last_tx_advanced = false;
    }
    if (flags & I2C_IC_INTR_STAT_R_RD_REQ_BITS) {
        const size_t before = host.response_cursor;
        const uint8_t byte = host_protocol_read(&host);
        last_tx_advanced = host.response_cursor > before;
        /* One byte per actual request, no payload prefetch at header STOP. */
        (void)hw->clr_rd_req;
        hw->data_cmd = byte;
    }
}

void host_i2c_init(uint8_t ready_mask)
{
    host_protocol_init(&host, ready_mask);
    qn_control_set_audio_sink(&host.qn,audio_engine_set_sources);
    last_tx_advanced = false;
    log_size=log_cursor=0;
    i2c_init(HOST_I2C, 100000);
    gpio_set_function(HOST_I2C_SDA_PIN, GPIO_FUNC_I2C);
    gpio_set_function(HOST_I2C_SCL_PIN, GPIO_FUNC_I2C);
    gpio_pull_up(HOST_I2C_SDA_PIN);
    gpio_pull_up(HOST_I2C_SCL_PIN);
    i2c_set_slave_mode(HOST_I2C, true, FMHEADEND_HOST_I2C_ADDRESS);
    i2c_hw_t *hw = i2c_get_hw(HOST_I2C);
    hw->enable = 0;
    hw->con |= I2C_IC_CON_STOP_DET_IFADDRESSED_BITS;
    hw->ack_general_call = 0;
    hw->dma_cr = 0;
    (void)hw->clr_intr;
    irq_set_exclusive_handler(I2C1_IRQ, host_i2c_irq);
    hw->intr_mask = I2C_IC_INTR_MASK_M_RX_FULL_BITS |
                    I2C_IC_INTR_MASK_M_RX_OVER_BITS |
                    I2C_IC_INTR_MASK_M_RD_REQ_BITS |
                    I2C_IC_INTR_MASK_M_RX_DONE_BITS |
                    I2C_IC_INTR_MASK_M_TX_ABRT_BITS |
                    I2C_IC_INTR_MASK_M_RESTART_DET_BITS |
                    I2C_IC_INTR_MASK_M_STOP_DET_BITS;
    irq_set_enabled(I2C1_IRQ, true);
    hw->enable = 1;
#if DEBUG_UART_ENABLED
    char line[128];
    snprintf(line, sizeof(line), "HOST I2C1: SDA=GPIO%u SCL=GPIO%u address=0x%02X; V1 QN CONTROL; RF OFF\r\n",
             HOST_I2C_SDA_PIN, HOST_I2C_SCL_PIN, FMHEADEND_HOST_I2C_ADDRESS);
    uart_puts(DEBUG_UART, line);
#endif
}

void host_i2c_poll(void)
{
    qn_control_audio_fault(&host.qn,audio_engine_state()!=AUDIO_RUNNING);
    host_work_t work;
    uint32_t lock=save_and_disable_interrupts();
    const bool have_work=host_protocol_take_work(&host,&work);
    restore_interrupts(lock);
    if (have_work) {
        host_protocol_run_work(&host,&work,(uint32_t)(time_us_64()/1000));
        qn_control_audio_fault(&host.qn,audio_engine_state()!=AUDIO_RUNNING);
        if (work.event.command==4 && work.event.status==HOST_OK) {
            work.result[0]=qn_control_fault(&host.qn);
            work.result[9]=host.qn.last_error;
            uint32_t errors=spi_audio_error_count();
            for (unsigned b=0;b<4;++b) work.result[5+b]=errors>>(8*b);
        }
        if (work.event.command==0x0c && work.event.status==HOST_OK) {
            uint32_t counts[8];
            spi_audio_diagnostics(counts);
            for (unsigned i=0;i<8;++i)
                for (unsigned b=0;b<4;++b) work.result[4*i+b]=counts[i]>>(8*b);
        }
        lock=save_and_disable_interrupts();
        host_protocol_complete_work(&host,&work);
#ifdef FMHEADEND_FLASH_UPDATE
        update_ack_armed=work.event.command==FU_COMMAND && (work.update_payload[0]==FU_ACTIVATE || work.update_payload[0]==FU_CONFIRM) &&
            work.event.status==HOST_OK && host.generation==work.generation;
        update_ack_command=work.update_payload[0];
        update_ack_generation=work.generation;
        update_ack_read=false;
#endif
        restore_interrupts(lock);
    }
    /* QN bus is serialized with host work on core0. Audio stays on core1;
     * host IRQs remain enabled throughout bounded RDS register operations. */
#ifdef FMHEADEND_FLASH_UPDATE
    if(update_ack_read && update_ack_armed) {
        update_ack_armed=false; update_ack_read=false;
        flash_update_ack_consumed(update_ack_command);
    }
    if(!flash_update_maintenance())
#endif
    rds_tx_service(&host.rds_tx,&host.qn,host.rds);
#if DEBUG_UART_ENABLED
    log_drain();
    if (log_cursor<log_size) return;
    log_cursor=log_size=0;
    if (!abrt_snapshot_printed && atomic_load_explicit(&abrt_snapshot_valid,memory_order_acquire)) {
        log_abort_snapshot();
        log_drain();
        return;
    }
#endif
    host_log_event_t event;
    const uint32_t saved = save_and_disable_interrupts();
    const bool have_event = host_protocol_pop_log(&host, &event);
    const uint32_t busy_count = host.log_busy_count;
    host.log_busy_count = 0;
    restore_interrupts(saved);
#if DEBUG_UART_ENABLED
    char line[320];
    if (busy_count) {
        snprintf(line, sizeof(line), "HOST LOG: %lu log events omitted (bounded UART queue)\r\n",
                 (unsigned long)busy_count);
        log_append(line);
    }
    if (!have_event) {
        if (spi_audio_diagnostic(line,sizeof(line),time_us_64()) ||
            rds_tx_diagnostic(&host.rds_tx,line,sizeof(line))) log_append(line);
        log_drain(); return;
    }
    const uint8_t *p = event.payload;
    if (event.command == 2 && !event.malformed && event.length == 15) {
        snprintf(line, sizeof(line), "HOST RX: SET_TX_CONFIG TX%u frequency_khz=%lu attenuation_x10=%u stereo=%u preemphasis_us=%u audio_mode=%u rds=%u gain_mode=%u analog_gain_x10=%d digital_trim_db=%u soft_clipping=%u\r\n",
                 event.target, (unsigned long)host_get_u32(p), host_get_u16(p + 4),
                 p[6], p[7], p[8], p[9], p[10], host_get_i16(p + 11), p[13], p[14]);
    } else if (event.command == 3 && !event.malformed && event.length == 1) {
        snprintf(line, sizeof(line), "HOST RX: SET_TX_ENABLED TX%u %s\r\n",
                 event.target, p[0] ? "ON" : "OFF");
    } else if ((event.command==7 || event.command==8) && !event.malformed &&
               event.length==(event.command==7?1:2)) {
        snprintf(line,sizeof(line),"HOST RX: %s TX%u reg=0x%02X value=0x%02X%s\r\n",
            host_command_name(event.command),event.target,p[0],p[1],
            event.command==7 && event.status!=HOST_OK ? " (read failed)" : "");
    } else if (event.command == 5 && event.status == HOST_OK) {
        snprintf(line, sizeof(line), "HOST RX: RDS_PUSH TX%u groups=%u free=%u (FIFO transition)\r\n",
                 event.target, p[0], p[1]);
    } else if (event.command == 6 && event.status == HOST_OK) {
        snprintf(line, sizeof(line), "HOST RX: RDS_CLEAR TX%u free=%u\r\n",
                 event.target, RDS_FIFO_CAPACITY);
    } else {
        snprintf(line, sizeof(line), "HOST RX: %s (0x%02X) target=0x%02X length=%u%s\r\n",
                 host_command_name(event.command), event.command, event.target,
                 event.length, event.malformed ? " INVALID FRAMING" : "");
    }
    log_append(line);
    snprintf(line, sizeof(line), "HOST TX: %s payload=%u bytes\r\n",
             host_status_name(event.status), event.response_length);
    log_append(line);
    log_drain();
#endif
}
