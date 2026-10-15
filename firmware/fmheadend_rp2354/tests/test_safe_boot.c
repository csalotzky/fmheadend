/* Run production main/boot with a register-level QN model and captured UART. */
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <string.h>

#define main firmware_main
#include "../fmheadend_rp2354.c"
#undef main

static bool output[32], latch[32];
static uint8_t addresses[QN_COUNT];
static uint8_t vga[QN_COUNT];
static bool vga_mismatch;
static unsigned operation, enabled_index, idle_ticks;
static int bad_index, fail_operation, invalid_cid_operation;
static bool tx_stuck, all_missing, initialized, led_values[5];
static char log_text[4096];
static jmp_buf idle_done;
static uint64_t now_us;
static bool host_started;
bool spi_audio_init(void) { return true; }
void flash_update_platform_init(bool ready) { assert(ready && initialized && !host_started); }
bool audio_engine_init(void)
{
    assert(!host_started);
    assert(initialized);
    for (unsigned i=0;i<QN_COUNT;++i)
        assert(!qn_boot_results[i].ready || addresses[i]==QN_FIRST_ADDR+i);
    return true;
}
audio_state_t audio_engine_state(void) { return AUDIO_RUNNING; }
void audio_engine_log_fault(void) { assert(false); }
const char *audio_engine_state_name(audio_state_t s) { assert(s==AUDIO_RUNNING); return "RUNNING"; }

void gpio_init(unsigned pin) { output[pin] = latch[pin] = false; }
void gpio_set_dir(unsigned pin, bool value) { output[pin] = value; }
void gpio_put(unsigned pin, bool value)
{
    if (pin == DIAGNOSTIC_LED_PIN) {
        if (host_started && ++idle_ticks == 5) longjmp(idle_done, 1);
        assert(idle_ticks < 5);
        led_values[idle_ticks] = value;
        return;
    }
    assert(pin >= 18 && pin <= 25);
    if (value) {
        assert(initialized && output[pin]);
        enabled_index = pin - 18;
        operation = 0;
        /* Previously assigned QNs may be on; no other default address may be. */
        for (unsigned i = 0; i < QN_COUNT; ++i) {
            assert(!latch[18 + i] || addresses[i] != QN_DEFAULT_ADDR);
        }
        addresses[enabled_index] = QN_DEFAULT_ADDR;
        vga[enabled_index] = 0x60;
    }
    latch[pin] = value;
}
void gpio_set_function(unsigned pin, unsigned function)
{
    assert((pin == 28 || pin == 29) && function == GPIO_FUNC_UART);
}
void sleep_ms(uint32_t ms)
{
    assert(ms == 1 || ms == 100 || ms == 10);
    now_us += (uint64_t)ms * 1000;
}
uint64_t time_us_64(void) { return now_us; }
void host_i2c_init(uint8_t ready_mask)
{
    for (unsigned i = 0; i < QN_COUNT; ++i) {
        assert(!!(ready_mask & (1u << i)) == qn_boot_results[i].ready);
    }
    host_started = true;
}
void host_i2c_poll(void) { assert(host_started); }
bool host_i2c_led(uint64_t now) { (void)now; return boot_hardware_fault && idle_ticks % 2 == 0; }
void uart_init(unsigned uart, unsigned baud)
{
    assert(uart == uart0 && baud == 115200);
    for (unsigned i = 18; i <= 25; ++i) assert(output[i] && !latch[i]);
}
void uart_set_hw_flow(unsigned uart, bool cts, bool rts)
{
    assert(uart == uart0 && !cts && !rts);
}
void uart_set_format(unsigned uart, unsigned bits, unsigned stop, unsigned parity)
{
    assert(uart == uart0 && bits == 8 && stop == 1 && parity == UART_PARITY_NONE);
}
void uart_puts(unsigned uart, const char *text)
{
    assert(uart == uart0 && strlen(log_text) + strlen(text) < sizeof(log_text));
    strcat(log_text, text);
}
void qn_bus_init(void) { initialized = true; }
const char *qn_bus_status_name(qn_bus_status_t status)
{
    assert(status == QN_BUS_NACK);
    return "NACK";
}

static qn_bus_status_t check_operation(uint8_t address, uint8_t reg, bool write)
{
    static const uint8_t registers[] = {6, 0, 2, 6, 0, 0, 4, 4};
    static const bool writes[] = {false, true, true, false, true, false, true, false};
    assert(operation < 8 && reg == registers[operation] && write == writes[operation]);
    assert(address == addresses[enabled_index]);
    const bool fail = all_missing || ((int)enabled_index == bad_index &&
                                      (int)operation == fail_operation);
    ++operation;
    return fail ? QN_BUS_NACK : QN_BUS_OK;
}

qn_bus_status_t qn_bus_read_reg(uint8_t address, uint8_t reg, uint8_t *value)
{
    const qn_bus_status_t status = check_operation(address, reg, false);
    if (status != QN_BUS_OK) return status;
    if (reg == 6) {
        *value = ((int)enabled_index == bad_index &&
                  (int)(operation - 1) == invalid_cid_operation) ? 0xff : 0x11;
    } else if (reg == 4) {
        *value = vga[enabled_index] ^ (vga_mismatch && (int)enabled_index == bad_index ? 1 : 0);
    } else {
        *value = tx_stuck && (int)enabled_index == bad_index ? 0x45 : 0x05;
    }
    return QN_BUS_OK;
}

qn_bus_status_t qn_bus_write_reg(uint8_t address, uint8_t reg, uint8_t value)
{
    const qn_bus_status_t status = check_operation(address, reg, true);
    if (reg == 2) {
        assert(value == QN_FIRST_ADDR + enabled_index);
        /* Deliberately model a write that takes effect even when ACK is lost. */
        addresses[enabled_index] = value;
    } else if (reg == 4) {
        assert(value == 0x45 && value == QN_REG_VGA_DEFAULT);
        vga[enabled_index] = value;
    } else {
        assert(reg == 0 && value == 0x05); /* No RF-enable/SWRST writes. */
    }
    return status;
}

static void run(int bad, int fail, int invalid_cid, bool stuck, bool missing)
{
    memset(output, 0, sizeof(output));
    memset(latch, 0, sizeof(latch));
    memset(qn_boot_results, 0, sizeof(qn_boot_results));
    memset(led_values, 0, sizeof(led_values));
    log_text[0] = '\0';
    boot_hardware_fault = initialized = false;
    idle_ticks = 0;
    now_us = 0;
    host_started = false;
    bad_index = bad;
    fail_operation = fail;
    invalid_cid_operation = invalid_cid;
    tx_stuck = stuck;
    all_missing = missing;
    if (setjmp(idle_done) == 0) firmware_main();

    const bool fault = bad >= 0 || missing;
    assert(boot_hardware_fault == fault && !led_values[0]);
    for (unsigned tick = 1; tick < 5; ++tick) {
        assert(led_values[tick] == (fault && tick % 2 == 1));
    }
    for (unsigned i = 0; i < QN_COUNT; ++i) {
        const bool ready = !missing && (int)i != bad;
        assert(qn_boot_results[i].ready == ready && latch[18 + i] == ready);
        char expected[64];
        if (ready) {
            assert(addresses[i] == 0x21 + i);
            assert(vga[i] == 0x45);
            snprintf(expected, sizeof(expected), "QN%u @ 0x%02X: CID2=0x11 OK, RF OFF", i, 0x21 + i);
        } else snprintf(expected, sizeof(expected), "QN%u target 0x%02X: FAIL", i, 0x21 + i);
        assert(strstr(log_text, expected));
    }
    assert(strstr(log_text, missing ? "BOOT: 0/8 ready; RF OFF; HARDWARE FAULT" :
                  fault ? "BOOT: 7/8 ready; RF OFF; HARDWARE FAULT" :
                          "BOOT: 8/8 ready; RF OFF; IDLE"));
}

int main(void)
{
    run(-1, -1, -1, false, false);
    for (int index = 0; index < 8; ++index) {
        for (int op = 0; op < 8; ++op) run(index, op, -1, false, false);
        run(index, -1, 0, false, false);
        run(index, -1, 3, false, false);
        run(index, -1, -1, true, false);
        vga_mismatch = true;
        run(index, -1, -1, false, false);
        assert(strstr(log_text, "crystal load verification"));
        vga_mismatch = false;
    }
    run(-1, -1, -1, false, true);
    assert(!qn_boot_device(8).ready);
    puts("Safe boot: 98 scenarios; crystal load, CEN isolation, RF OFF, failures, UART and LED PASS");
}
