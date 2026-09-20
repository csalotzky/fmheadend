#ifndef TEST_HARDWARE_IRQ_H
#define TEST_HARDWARE_IRQ_H
#include <stdbool.h>
#define I2C1_IRQ 1
void irq_set_exclusive_handler(unsigned irq, void (*handler)(void));
void irq_set_enabled(unsigned irq, bool enabled);
#endif
