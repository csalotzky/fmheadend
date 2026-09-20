#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "../host_i2c.c"

i2c_inst_t test_i2c1;
static unsigned available;
static bool in_irq, masked;
static void (*handler)(void);
static char uart_log[4096];
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
uint64_t time_us_64(void) { return 1234567000; }
uint32_t save_and_disable_interrupts(void) { assert(!in_irq && !masked); masked = true; return 42; }
void restore_interrupts(uint32_t saved) { assert(masked && saved == 42); masked = false; }
void uart_puts(unsigned uart, const char *text)
{
    assert(uart == uart0 && !in_irq && !masked);
    assert(strlen(uart_log) + strlen(text) < sizeof(uart_log));
    strcat(uart_log, text);
}
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
static void finish_read(void)
{
    irq(I2C_IC_INTR_STAT_R_RX_DONE_BITS | I2C_IC_INTR_STAT_R_STOP_DET_BITS, 0, 0);
}
int main(void)
{
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
    finish_read();
    host_i2c_poll();
    assert(strstr(uart_log, "HOST RX: GET_INFO") && strstr(uart_log, "HOST TX: OK payload=35 bytes"));

    const uint8_t on[] = {3,3,1,1};
    write_request(on, sizeof(on));
    assert(read_byte() == 0 && read_byte() == 0);
    finish_read();
    host_i2c_poll();
    assert(strstr(uart_log, "HOST RX: SET_TX_ENABLED TX3 ON"));
    assert(strstr(uart_log, "HOST TX: STUB OK - NOT EXECUTED"));
    const uint8_t config[] = {2,3,15,0x34,0x7f,1,0,60,0,1,50,0,0,1,0xf1,0xff,1,1};
    write_request(config, sizeof(config));
    host_i2c_poll();
    assert(strstr(uart_log, "SET_TX_CONFIG TX3 frequency_khz=98100"));
    assert(strstr(uart_log, "analog_gain_x10=-15"));

    const uint8_t status[] = {4,255,0};
    write_request(status, sizeof(status));
    assert(read_byte() == 0 && read_byte() == 130);
    finish_read();
    for (unsigned i = 0; i < 130; ++i) {
        const uint8_t byte = read_byte();
        if (i >= 10 && (i - 10) % 15 == 0) assert(byte == 0);
    }
    finish_read();
    host_i2c_poll();

    write_request(info, sizeof(info));
    assert(read_byte() == 0); /* Queued byte is flushed rather than sent. */
    test_i2c1.hw.tx_abrt_source = 1u << 23;
    irq(I2C_IC_INTR_STAT_R_TX_ABRT_BITS, 0, 0);
    assert(read_byte() == 0 && read_byte() == 35);
    finish_read();
    host_i2c_poll();

    irq(I2C_IC_INTR_STAT_R_RX_FULL_BITS, 1, 3 | I2C_IC_DATA_CMD_FIRST_DATA_BYTE_BITS);
    irq(I2C_IC_INTR_STAT_R_RESTART_DET_BITS, 0, 0);
    assert(read_byte() == HOST_INVALID_ARGUMENT && read_byte() == 0);
    finish_read();
    host_i2c_poll();
    assert(strstr(uart_log, "INVALID FRAMING"));
    irq(I2C_IC_INTR_STAT_R_RX_OVER_BITS | I2C_IC_INTR_STAT_R_STOP_DET_BITS, 0, 0);
    assert(read_byte() == HOST_INVALID_ARGUMENT);
    host_i2c_poll();
    write_request(info, sizeof(info));
    assert(read_byte() == 0 && read_byte() == 35);
    puts("I2C adapter: pins/address, RX+STOP ordering, split reads, UART outside IRQ, stubs PASS");
}
