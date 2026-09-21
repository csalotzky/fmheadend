#pragma once
#include <stdint.h>
static inline void __dmb(void) {}
void hw_clear_bits(uint32_t *p,uint32_t bits);
void hw_set_bits(uint32_t *p,uint32_t bits);

uint32_t save_and_disable_interrupts(void);
void restore_interrupts(uint32_t);
