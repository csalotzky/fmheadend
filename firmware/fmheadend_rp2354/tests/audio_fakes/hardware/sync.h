#pragma once
#include <stdint.h>
static inline void __dmb(void) {}
static inline void hw_clear_bits(uint32_t *p,uint32_t bits) { *p &= ~bits; }
static inline void hw_set_bits(uint32_t *p,uint32_t bits) { *p |= bits; }
