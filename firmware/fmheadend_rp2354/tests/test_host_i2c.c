#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "../host_i2c.c"
#include "fake_qn.h"
#include "host_test_frame.h"

i2c_inst_t test_i2c1;
static unsigned available;
static bool in_irq, masked;
static void (*handler)(void);
static char uart_log[4096];
static audio_state_t audio_state=AUDIO_RUNNING;
static uint8_t audio_mask, digital_mask;
uint32_t spi_audio_error_count(void) { return 0x12345678; }
bool spi_audio_diagnostic(char *s,size_t n,uint64_t now) { (void)s;(void)n;(void)now;return false; }
audio_state_t audio_engine_state(void) { assert(!in_irq && !masked); return audio_state; }
void audio_engine_set_sources(uint8_t mask, uint8_t digital) { assert(!in_irq && !masked); audio_mask=mask; digital_mask=digital; }
i2c_hw_t *i2c_get_hw(i2c_inst_t *i2c) { assert(i2c == i2c1); return &i2c->hw; }
unsigned i2c_get_read_available(i2c_inst_t *i2c) { assert(i2c == i2c1); return available; }
void i2c_init(i2c_inst_t *i2c, unsigned baud) { assert(i2c == i2c1 && baud == 100000); }
void i2c_set_slave_mode(i2c_inst_t *i2c, bool slave, uint8_t address)
{
    assert(i2c == i2c1 && slave && address == 0x62);
}
void gpio_set_function(unsigned pin, unsigned function)
{
    assert((pin == 14 || pin == 15) && function == GPIO_FUNC_I2C);
}
void gpio_pull_up(unsigned pin) { assert(pin == 14 || pin == 15); }
void irq_set_exclusive_handler(unsigned irq, void (*fn)(void)) { assert(irq == I2C1_IRQ); handler = fn; }
void irq_set_enabled(unsigned irq, bool enabled) { assert(irq == I2C1_IRQ && enabled); }
uint64_t time_us_64(void) { return 1234567000+fake_now; }
uint32_t save_and_disable_interrupts(void) { assert(!in_irq && !masked); masked = true; return 42; }
void restore_interrupts(uint32_t saved) { assert(masked && saved == 42); masked = false; }
void uart_puts(unsigned uart, const char *text)
{
    assert(uart == uart0 && !in_irq && !masked);
    assert(strlen(uart_log) + strlen(text) < sizeof(uart_log));
    strcat(uart_log, text);
}
bool uart_is_writable(unsigned uart) { assert(uart==uart0 && !in_irq && !masked); return true; }
void uart_putc_raw(unsigned uart,char c) { char s[]={c,0};uart_puts(uart,s); }
static void foreground_only(void) { assert(!in_irq && !masked); }
static void drain(void) { for(unsigned i=0;i<100;++i) host_i2c_poll(); }
static void irq(uint32_t flags, unsigned rx, uint32_t byte)
{
    assert(!masked);
    available = rx;
    test_i2c1.hw.intr_stat = flags;
    test_i2c1.hw.data_cmd = byte;
    in_irq = true;
    handler();
    in_irq = false;
    available = 0;
}
static void write_request(const uint8_t *r, size_t n)
{
    uint8_t frame[HOST_REQUEST_MAX];
    assert(n + 2 <= sizeof(frame));
    memcpy(frame, r, n);
    uint16_t crc=host_crc16(r,n);
    frame[n++]=(uint8_t)crc; frame[n++]=(uint8_t)(crc>>8);
    r=frame;
    for (size_t i = 0; i < n; ++i) {
        irq(I2C_IC_INTR_STAT_R_RX_FULL_BITS |
            (i == n - 1 ? I2C_IC_INTR_STAT_R_STOP_DET_BITS : 0), 1,
            r[i] | (i == 0 ? I2C_IC_DATA_CMD_FIRST_DATA_BYTE_BITS : 0));
    }
}
static uint8_t read_byte(void)
{
    irq(I2C_IC_INTR_STAT_R_RD_REQ_BITS, 0, 0);
    return test_i2c1.hw.data_cmd;
}
static void read_crc(void)
{
    assert(host.response_cursor == host.response_size - 2);
    test_response_crc(&host);
    assert(read_byte() == host.response[host.response_size - 2]);
    assert(read_byte() == host.response[host.response_size - 1]);
}
static void finish_read(void)
{
    irq(I2C_IC_INTR_STAT_R_RX_DONE_BITS | I2C_IC_INTR_STAT_R_STOP_DET_BITS, 0, 0);
}
int main(void)
{
    fake_qn_reset();fake_operation_hook=foreground_only;
    host_i2c_init(0xff);
    assert(test_i2c1.hw.enable == 1 && test_i2c1.hw.ack_general_call == 0);
    assert(test_i2c1.hw.con & I2C_IC_CON_STOP_DET_IFADDRESSED_BITS);
    assert(strstr(uart_log, "address=0x62"));
    uart_log[0] = 0;
    const uint8_t info[] = {1,255,0};
    write_request(info, sizeof(info)); /* Final RX + STOP in the same IRQ. */
    assert(uart_log[0] == 0);
    assert(read_byte() == 0 && read_byte() == 35);
    finish_read();
    assert(read_byte() == 1 && read_byte() == 0);
    for (unsigned i = 2; i < 35; ++i) assert(read_byte() == host.response[i + 2]);
    read_crc();
    finish_read();
    drain();
    assert(strstr(uart_log, "HOST RX: GET_INFO") && strstr(uart_log, "HOST TX: OK payload=35 bytes"));

    const uint8_t on[] = {3,3,1,1};
    write_request(on, sizeof(on));
    assert(host.pending); drain();
    assert(read_byte() == HOST_INVALID_ARGUMENT && read_byte() == 0);
    finish_read();
    drain();
    assert(strstr(uart_log, "HOST RX: SET_TX_ENABLED TX3 ON"));
    assert(strstr(uart_log, "HOST TX: INVALID_ARGUMENT"));
    const uint8_t config[] = {2,3,15,0x34,0x7f,1,0,60,0,1,50,0,0,1,0xf1,0xff,1,1};
    write_request(config, sizeof(config));
    drain();
    assert(strstr(uart_log, "SET_TX_CONFIG TX3 frequency_khz=98100"));
    assert(strstr(uart_log, "analog_gain_x10=-15"));
    assert(digital_mask==8 && !host.qn.channel[3].enabled);

    const uint8_t status[] = {4,255,0};
    write_request(status, sizeof(status));
    assert(host.pending); drain();
    assert(read_byte() == 0 && read_byte() == 130);
    finish_read();
    for (unsigned i = 0; i < 130; ++i) {
        const uint8_t byte = read_byte();
        if (i==0 || i==9) assert(byte==0); /* SPI errors are not a critical QN/audio fault. */
        if (i>=5 && i<9) assert(byte==(uint8_t)(0x12345678u>>(8*(i-5))));
        if (i >= 10 && (i - 10) % 15 == 0) assert(byte == 0);
    }
    read_crc();
    finish_read();
    drain();

    /* Maximum RDS batch ACK is ready at write STOP, without foreground work.
     * IRQ mocks assert no UART/QN/audio calls; real source/RF state stays intact. */
    uart_log[0]=0;
    unsigned calls_before=fake_calls;
    const qn_control_t qn_before=host.qn;
    uint8_t rds_push[252]={5,3,249,31};
    for(unsigned i=4;i<sizeof(rds_push);++i) rds_push[i]=(uint8_t)i;
    write_request(rds_push,sizeof(rds_push));
    assert(!host.pending && !uart_log[0]);
    assert(read_byte()==HOST_OK && read_byte()==0);finish_read();
    read_crc();finish_read();
    const uint8_t rds_free[]={11,255,0};
    write_request(rds_free,sizeof(rds_free));
    assert(read_byte()==HOST_OK && read_byte()==16);finish_read();
    for(unsigned i=0;i<8;++i) { assert(read_byte()==(i==3?33:64));assert(read_byte()==0); }
    read_crc();
    finish_read();
    assert(fake_calls==calls_before && !memcmp(&qn_before,&host.qn,sizeof(qn_before)));
    assert(digital_mask==8 && audio_mask==0 && !host_i2c_led(0));
    drain();
    assert(strstr(uart_log,"RDS_PUSH TX3 groups=31 free=33"));
    assert(!strstr(uart_log,"GET_RDS_FIFO_FREE"));
    write_request(status,sizeof(status));drain();
    assert(host_get_u16(host.response+2+10+15*3+11)==33);
    assert(host_get_u32(host.response+2+10+15*3+7)==0);
    const uint8_t rds_clear[]={6,3,0};
    calls_before=fake_calls;
    write_request(rds_clear,sizeof(rds_clear));
    assert(read_byte()==HOST_OK && read_byte()==0);finish_read();
    read_crc();finish_read();
    assert(fake_calls==calls_before && rds_fifo_free(&host.rds[3])==64);
    assert(digital_mask==8 && !host.qn.channel[3].enabled);
    drain();uart_log[0]=0;

    write_request(info, sizeof(info));
    assert(read_byte() == 0); /* Queued byte is flushed rather than sent. */
    test_i2c1.hw.tx_abrt_source = (1u << 23) | 0x2000;
    test_i2c1.hw.raw_intr_stat = 0x240;
    test_i2c1.hw.status = 0x64;
    irq(I2C_IC_INTR_STAT_R_TX_ABRT_BITS, 0, 0);
    assert(atomic_load(&abrt_snapshot_valid) && !abrt_snapshot_printed);
    assert(!uart_log[0]); /* Snapshot only; UART mocks also reject ISR printing. */
    assert(abrt_snapshot.timestamp_us == time_us_64());
    assert(abrt_snapshot.cursor == 1 && host.response_cursor == 0);
    assert(abrt_snapshot.last_tx_advanced && !last_tx_advanced);
    assert(abrt_snapshot.flags == 0x40 && abrt_snapshot.intr == 0x40);
    assert(abrt_snapshot.raw == 0x240 && abrt_snapshot.status == 0x64);
    assert(abrt_snapshot.txflr == 0 && abrt_snapshot.rxflr == 0);
    assert(!abrt_snapshot.receiving && !abrt_snapshot.pending && !abrt_snapshot.running);
    assert(abrt_snapshot.size == 39 && abrt_snapshot.copied == 39);
    assert(!memcmp(abrt_snapshot.response,host.response,39));
    assert(abrt_snapshot.response[39] == 0);
    assert(read_byte() == 0 && read_byte() == 35);
    finish_read();
    /* Replace the live response and abort again before any foreground print. */
    write_request(rds_free,sizeof(rds_free));
    test_i2c1.hw.tx_abrt_source = 0x5000;
    irq(I2C_IC_INTR_STAT_R_TX_ABRT_BITS,0,0);
    assert(abrt_snapshot.source == 0x00802000 && abrt_snapshot.cursor == 1);
    assert(abrt_snapshot.response[1] == 35 && host.response[1] == 16);
    drain();
    assert(abrt_snapshot_printed);
    const char *snapshot_log=strstr(uart_log,"HOST I2C ABRT SNAP:");
    assert(snapshot_log && !strstr(snapshot_log+1,"HOST I2C ABRT SNAP:"));
    assert(strstr(snapshot_log,"source=0x00802000 flags=0x00000040 raw=0x00000240 intr=0x00000040"));
    assert(strstr(snapshot_log,"cursor=1 size=39 last_tx_advanced=1 receiving=0"));
    assert(strstr(snapshot_log,"flush_cnt=1 copied=39\r\nresponse=00 23 01 00 30 2E"));
    uart_log[0]=0;
    irq(I2C_IC_INTR_STAT_R_TX_ABRT_BITS,0,0);
    drain();
    assert(!strstr(uart_log,"HOST I2C ABRT SNAP:"));

    irq(I2C_IC_INTR_STAT_R_RX_FULL_BITS, 1, 3 | I2C_IC_DATA_CMD_FIRST_DATA_BYTE_BITS);
    irq(I2C_IC_INTR_STAT_R_RESTART_DET_BITS, 0, 0);
    assert(read_byte() == HOST_INVALID_ARGUMENT && read_byte() == 0);
    finish_read();
    drain();
    assert(strstr(uart_log, "INVALID FRAMING"));
    irq(I2C_IC_INTR_STAT_R_RX_OVER_BITS | I2C_IC_INTR_STAT_R_STOP_DET_BITS, 0, 0);
    assert(read_byte() == HOST_INVALID_ARGUMENT);
    drain();
    write_request(info, sizeof(info));
    assert(read_byte() == 0 && read_byte() == 35);
    finish_read(); drain(); uart_log[0]=0;
    uint8_t sine_config[sizeof(config)]; memcpy(sine_config,config,sizeof(config));
    sine_config[3+8]=2;
    write_request(sine_config,sizeof(sine_config)); drain(); assert(audio_mask==0);
    write_request(on,sizeof(on)); drain(); assert(audio_mask==(1u<<3));
    calls_before=fake_calls;
    write_request(rds_push,sizeof(rds_push));
    assert(read_byte()==HOST_OK && read_byte()==0);finish_read();
    read_crc();finish_read();
    write_request(rds_clear,sizeof(rds_clear));
    assert(read_byte()==HOST_OK && read_byte()==0);finish_read();
    read_crc();finish_read();
    assert(fake_calls==calls_before && host.qn.channel[3].enabled);
    assert(audio_mask==(1u<<3) && host_i2c_led(0));
    drain();
    /* Exercise the actual foreground RDS adapter with IRQ guards active.
     * A running SINE lane is unaffected by FIFO service and acknowledgement. */
    uart_log[0]=0;
    sine_config[3+9]=1;
    write_request(sine_config,sizeof(sine_config));drain();
    fake_now+=QN_RDS_STARTUP_US;
    write_request(rds_push,sizeof(rds_push));drain();
    assert(host.rds_tx.channel[3].phase==RDS_WAIT);
    assert(host.rds[3].count==31 && audio_mask==(1u<<3));
    fake_regs[3][0x1b]^=0x80;
    host.rds_tx.channel[3].next_poll=0;
    host_i2c_poll();
    assert(host.rds[3].count==30 && host.qn.channel[3].enabled);
    write_request(rds_clear,sizeof(rds_clear));drain();
    assert(host.rds[3].count==0 && audio_mask==(1u<<3));
    uart_log[0]=0;
    audio_state=AUDIO_STALL_FAULT;
    write_request(status,sizeof(status)); drain();
    assert(read_byte()==HOST_OK && read_byte()==130); finish_read();
    assert(read_byte()==1);
    for (unsigned i=1;i<9;++i) (void)read_byte();
    assert(read_byte()==HOST_INTERNAL_ERROR);
    assert(host.qn.channel[3].enabled); /* Audio failure never fabricates RF OFF. */
    assert(!host_i2c_led(0) && host_i2c_led(250000));
    puts("I2C adapter: pins/address, split reads, foreground QN/audio, source selection, real audio fault PASS");
    return 0;
}
