#ifndef FMHEADEND_BOARD_H
#define FMHEADEND_BOARD_H

/* Verified PCB mapping: RP GPIO numbers, not package pin numbers. */
#define AUDIO_SPI_MOSI_PIN 0u
#define AUDIO_SPI_CS_PIN 1u
#define AUDIO_SPI_SCK_PIN 2u
#define AUDIO_SPI_MISO_PIN 3u /* Left as input: RX-only audio transport. */
#define I2S_DATA_FIRST_PIN 4u
#define I2S_BCLK_PIN 12u
#define I2S_WS_PIN 13u
#define HOST_I2C_SDA_PIN 14u
#define HOST_I2C_SCL_PIN 15u
#define QN_SDA_PIN 16u
#define QN_SCL_PIN 17u
#define QN_COUNT 8u
/* XTM32026000GT00351001: 26 MHz, CL=10 pF, no external load capacitors.
 * Board-calibrated REG_VGA: RIN[7:6]=01 (20 kohm, unchanged), XCSEL[5:0]=5.
 * See docs/milestone8_3.md, Crystal-load calibration.
 */
#define QN_REG_VGA_DEFAULT 0x45u
#define QN_CEN_FIRST_PIN 18u
#define DIAGNOSTIC_LED_PIN 27u
#define DEBUG_UART uart0
#define DEBUG_UART_TX_PIN 28u
#define DEBUG_UART_RX_PIN 29u
#define DEBUG_UART_BAUD 115200u

#endif
