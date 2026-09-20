#ifndef FMHEADEND_BOARD_H
#define FMHEADEND_BOARD_H

/* Verified PCB mapping: RP GPIO numbers, not package pin numbers. */
#define HOST_I2C_SDA_PIN 14u
#define HOST_I2C_SCL_PIN 15u
#define QN_SDA_PIN 16u
#define QN_SCL_PIN 17u
#define QN_COUNT 8u
#define QN_CEN_FIRST_PIN 18u
#define DIAGNOSTIC_LED_PIN 27u
#define DEBUG_UART uart0
#define DEBUG_UART_TX_PIN 28u
#define DEBUG_UART_RX_PIN 29u
#define DEBUG_UART_BAUD 115200u

#endif
