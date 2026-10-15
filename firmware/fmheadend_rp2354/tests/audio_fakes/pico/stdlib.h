#pragma once
#include <stdint.h>
uint64_t time_us_64(void);
void tight_loop_contents(void);

#include <stdbool.h>
typedef unsigned uint;
#define GPIO_IN 0
#define GPIO_IRQ_EDGE_RISE 8
#define GPIO_IRQ_EDGE_FALL 4
bool gpio_get(unsigned);
void gpio_init(unsigned);
void gpio_set_dir(unsigned,bool);
void gpio_pull_up(unsigned);
void gpio_acknowledge_irq(unsigned,uint32_t);
void gpio_set_irq_enabled(unsigned,uint32_t,bool);
void gpio_set_irq_enabled_with_callback(unsigned,uint32_t,bool,void (*)(unsigned,uint32_t));
