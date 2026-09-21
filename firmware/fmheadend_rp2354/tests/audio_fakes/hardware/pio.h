#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct { uint32_t fdebug, txf[4]; } fake_pio_t;
extern fake_pio_t fake_pio;
typedef fake_pio_t *PIO;
#define pio0 (&fake_pio)
#define PIO_FIFO_JOIN_TX 1
#define PIO_FDEBUG_TXSTALL_LSB 24
#define pio_x 1
typedef struct { unsigned out, count, side, divider; bool right, autopull; } pio_sm_config;
typedef struct { unsigned unused; } pio_program_t;
int pio_claim_unused_sm(PIO,bool);
bool pio_can_add_program(PIO,const pio_program_t *);
unsigned pio_add_program(PIO,const pio_program_t *);
void pio_sm_unclaim(PIO,unsigned);
void pio_sm_init(PIO,unsigned,unsigned,const pio_sm_config *);
void pio_sm_exec(PIO,unsigned,unsigned);
void pio_sm_set_enabled(PIO,unsigned,bool);
void pio_sm_set_pins_with_mask(PIO,unsigned,uint32_t,uint32_t);
void pio_sm_set_consecutive_pindirs(PIO,unsigned,unsigned,unsigned,bool);
void pio_gpio_init(PIO,unsigned);
bool pio_sm_is_tx_fifo_full(PIO,unsigned);
unsigned pio_get_dreq(PIO,unsigned,bool);
static inline unsigned pio_encode_set(unsigned dest,unsigned v) { return dest*32+v; }
static inline void sm_config_set_out_pins(pio_sm_config *c,unsigned p,unsigned n) { c->out=p;c->count=n; }
static inline void sm_config_set_sideset_pins(pio_sm_config *c,unsigned p) { c->side=p; }
static inline void sm_config_set_out_shift(pio_sm_config *c,bool r,bool a,unsigned n) { c->right=r;c->autopull=a;(void)n; }
static inline void sm_config_set_fifo_join(pio_sm_config *c,unsigned f) { (void)c;(void)f; }
static inline void sm_config_set_clkdiv_int_frac8(pio_sm_config *c,unsigned i,unsigned f) { c->divider=i*256+f; }
