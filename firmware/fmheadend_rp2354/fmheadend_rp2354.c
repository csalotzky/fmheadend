#include <stdarg.h>
#include <stdio.h>

#include "pico/stdlib.h"
#include "hardware/uart.h"
#include "board.h"
#include "qn_boot.h"
#include "host_i2c.h"
#include "audio_engine.h"
#include "spi_audio.h"

/* Inspectable via SWD; Boot observations remain available independently of runtime control. */
qn_boot_result_t qn_boot_results[QN_COUNT];
bool boot_hardware_fault;

static void uart_printf(const char *format, ...)
{
    char buffer[192];
    va_list args;
    va_start(args, format);
    vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    uart_puts(DEBUG_UART, buffer);
}

int main(void)
{
    qn_boot_hold_all_reset();
    gpio_init(DIAGNOSTIC_LED_PIN);
    gpio_put(DIAGNOSTIC_LED_PIN, 0);
    gpio_set_dir(DIAGNOSTIC_LED_PIN, GPIO_OUT);

    uart_init(DEBUG_UART, DEBUG_UART_BAUD);
    uart_set_hw_flow(DEBUG_UART, false, false);
    uart_set_format(DEBUG_UART, 8, 1, UART_PARITY_NONE);
    gpio_set_function(DEBUG_UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(DEBUG_UART_RX_PIN, GPIO_FUNC_UART);
    uart_puts(DEBUG_UART, "\r\nfmheadend RP2354 M8.6A; M8.1 safe boot; RF OFF\r\n");

    qn_bus_init();
    sleep_ms(100);
    unsigned ready_count = 0;
    uint8_t ready_mask = 0;
    for (unsigned index = 0; index < QN_COUNT; ++index) {
        qn_boot_results[index] = qn_boot_device(index);
        const qn_boot_result_t *result = &qn_boot_results[index];
        if (result->ready) {
            ++ready_count;
            ready_mask |= 1u << index;
            uart_printf("QN%u @ 0x%02X: CID2=0x%02X OK, RF OFF\r\n",
                        index, result->address, result->cid2);
        } else {
            boot_hardware_fault = true;
            uart_printf("QN%u target 0x%02X: FAIL %s (%s); ", index,
                        result->address, result->stage,
                        result->bus_status == QN_BUS_OK ? "mismatch" :
                        qn_bus_status_name(result->bus_status));
            if (result->cid2_read) uart_printf("CID2=0x%02X; ", result->cid2);
            else uart_puts(DEBUG_UART, "CID2=unavailable; ");
            uart_puts(DEBUG_UART, "CEN LOW, RF OFF\r\n");
        }
    }
    uart_printf("BOOT: %u/%u ready; RF OFF; %s\r\n", ready_count, QN_COUNT,
                boot_hardware_fault ? "HARDWARE FAULT" : "IDLE");
    spi_audio_init(); /* Resource failure leaves DIGITAL silent; QN/RF control remains usable. */
    audio_engine_init();
    audio_state_t audio_state=audio_engine_state();
    uart_printf("AUDIO: %s; 48000 Hz stereo S16; BCLK=1536000 Hz; DATA=GPIO4..11\r\n",
                audio_engine_state_name(audio_state));
    host_i2c_init(ready_mask);

    /* Runtime LED includes active RF and current hardware faults. */
    bool led_on = false;
    uint64_t next_led_us = time_us_64() + 250000;
    while (true) {
        host_i2c_poll();
        if (audio_engine_state()!=audio_state) {
            audio_state=audio_engine_state();
            uart_printf("AUDIO: %s; reset required\r\n",audio_engine_state_name(audio_state));
            audio_engine_log_fault();
        }
        const uint64_t now = time_us_64();
        if (now >= next_led_us) {
            led_on = host_i2c_led(now);
            gpio_put(DIAGNOSTIC_LED_PIN, led_on);
            next_led_us = now + 250000;
        }
        sleep_ms(1);
    }
}
