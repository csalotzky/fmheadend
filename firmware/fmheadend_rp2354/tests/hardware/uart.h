#ifndef TEST_HARDWARE_UART_H
#define TEST_HARDWARE_UART_H
#include <stdbool.h>
#define uart0 0
#define UART_PARITY_NONE 0
void uart_init(unsigned uart, unsigned baud);
void uart_set_hw_flow(unsigned uart, bool cts, bool rts);
void uart_set_format(unsigned uart, unsigned bits, unsigned stop, unsigned parity);
bool uart_is_writable(unsigned uart);
void uart_putc_raw(unsigned uart, char c);
void uart_puts(unsigned uart, const char *text);
#endif
