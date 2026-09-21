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

#if !defined(FMHEADEND_HOST_I2C_ADDRESS) || FMHEADEND_HOST_I2C_ADDRESS < 0x08 || FMHEADEND_HOST_I2C_ADDRESS > 0x77
#error "Configure an unreserved 7-bit FMHEADEND_HOST_I2C_ADDRESS"
#endif

#define HOST_I2C i2c1
static host_protocol_t host;
static bool last_tx_advanced;
static char log_line[512];
static size_t log_size, log_cursor;
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
    char line[128];
    snprintf(line, sizeof(line), "HOST I2C1: SDA=GPIO%u SCL=GPIO%u address=0x%02X; V1 M8.3 QN CONTROL; RF OFF\r\n",
             HOST_I2C_SDA_PIN, HOST_I2C_SCL_PIN, FMHEADEND_HOST_I2C_ADDRESS);
    uart_puts(DEBUG_UART, line);
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
        lock=save_and_disable_interrupts();
        host_protocol_complete_work(&host,&work);
        restore_interrupts(lock);
    }
    /* QN bus is serialized with host work on core0. Audio stays on core1;
     * host IRQs remain enabled throughout bounded RDS register operations. */
    rds_tx_service(&host.rds_tx,&host.qn,host.rds);
    log_drain();
    if (log_cursor<log_size) return;
    log_cursor=log_size=0;
    host_log_event_t event;
    const uint32_t saved = save_and_disable_interrupts();
    const bool have_event = host_protocol_pop_log(&host, &event);
    const uint32_t busy_count = host.log_busy_count;
    host.log_busy_count = 0;
    restore_interrupts(saved);
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
}
