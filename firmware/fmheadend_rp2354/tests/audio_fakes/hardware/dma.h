#pragma once
#include <stdbool.h>
#include <stdint.h>
#define DMA_CH0_CTRL_TRIG_EN_BITS 1u
#define DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS (1u<<31)
#define DMA_CH0_CTRL_TRIG_BUSY_BITS (1u<<26)
#define DMA_SIZE_32 2
typedef struct { uint32_t ctrl_trig, al1_ctrl, transfer_count, read_addr, write_addr; } fake_dma_channel_t;
typedef struct { fake_dma_channel_t ch[4]; uint32_t intr,abort; } fake_dma_t;
extern fake_dma_t fake_dma;
#define dma_hw (&fake_dma)
typedef struct { unsigned chain,ring,dreq,size; bool read,write,priority; } dma_channel_config;
int dma_claim_unused_channel(bool);
void dma_channel_unclaim(unsigned);
void dma_channel_configure(unsigned,const dma_channel_config *,volatile void *,const void *,uint32_t,bool);
void dma_start_channel_mask(uint32_t);
bool dma_channel_is_busy(unsigned);
void dma_channel_set_read_addr(unsigned,const void *,bool);
void dma_channel_set_trans_count(unsigned,uint32_t,bool);
static inline uint32_t dma_encode_transfer_count(uint32_t n) { return n; }
static inline dma_channel_config dma_channel_get_default_config(unsigned ch) { (void)ch;return (dma_channel_config){0}; }
static inline void channel_config_set_transfer_data_size(dma_channel_config *c,unsigned n) { c->size=n; }
static inline void channel_config_set_read_increment(dma_channel_config *c,bool n) { c->read=n; }
static inline void channel_config_set_write_increment(dma_channel_config *c,bool n) { c->write=n; }
static inline void channel_config_set_dreq(dma_channel_config *c,unsigned n) { c->dreq=n; }
static inline void channel_config_set_chain_to(dma_channel_config *c,unsigned n) { c->chain=n; }
static inline void channel_config_set_ring(dma_channel_config *c,bool write,unsigned n) { c->ring=n;c->write=write; }
static inline void channel_config_set_high_priority(dma_channel_config *c,bool n) { c->priority=n; }

#define DMA_CH0_CTRL_TRIG_READ_ERROR_BITS (1u<<30)
#define DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS (1u<<29)
#define DMA_SIZE_8 0
void dma_channel_set_write_addr(unsigned,void *,bool);
