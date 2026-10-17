#ifndef TEST_PICO_STDLIB_H
#define TEST_PICO_STDLIB_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define GPIO_IN false
#define GPIO_OUT true
#define GPIO_FUNC_UART 2
#define GPIO_FUNC_I2C 3
void gpio_init(unsigned pin);
void gpio_put(unsigned pin, bool value);
void gpio_set_dir(unsigned pin, bool output);
void gpio_pull_up(unsigned pin);
void gpio_disable_pulls(unsigned pin);
bool gpio_get(unsigned pin);
void gpio_set_function(unsigned pin, unsigned function);
void sleep_us(uint64_t us);
void sleep_ms(uint32_t ms);
uint64_t time_us_64(void);
#endif
