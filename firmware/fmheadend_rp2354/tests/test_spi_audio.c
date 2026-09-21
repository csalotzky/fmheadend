/* Production CS/DMA adapter against bounded register/FIFO fakes. PIO wire
 * sampling is verified separately by the instruction timing test. */
#include <assert.h>
#include <string.h>
#include "../spi_audio.c"
fake_pio_t fake_pio;
fake_dma_t fake_dma;
static bool cs=true,in_irq,masked,enabled,abort_stuck;
static uint64_t now;
static unsigned fifo_count, fifo_at, starts, fail_resource;
static uint32_t fifo[8], sampled_bits;
static uint8_t *destination;
static void (*callback)(unsigned,uint32_t);
uint64_t time_us_64(void) { if(!abort_stuck) fake_dma.abort=0;return ++now; }
bool gpio_get(unsigned p) { assert(p==1);return cs; }
void gpio_init(unsigned p) { assert(p<4); }
void gpio_set_dir(unsigned p,bool out) { assert(p<4 && !out); }
void gpio_pull_up(unsigned p) { assert(p==1); }
void gpio_acknowledge_irq(unsigned p,uint32_t e) { assert(p==1 && e==12); }
void gpio_set_irq_enabled_with_callback(unsigned p,uint32_t e,bool on,void (*fn)(unsigned,uint32_t))
{ assert(p==1 && e==12 && on);callback=fn; }
uint32_t clock_get_hz(unsigned c) { (void)c;return fail_resource==1?125000000:150000000; }
int pio_claim_unused_sm(PIO p,bool required) { assert(p==pio1 && !required);return fail_resource==2?-1:2; }
bool pio_can_add_program(PIO p,const pio_program_t *pr) { (void)p;(void)pr;return fail_resource!=3; }
int pio_add_program(PIO p,const pio_program_t *pr) { (void)p;(void)pr;return 7; }
void pio_remove_program(PIO p,const pio_program_t *pr,unsigned off) { (void)p;(void)pr;assert(off==7); }
void pio_sm_unclaim(PIO p,unsigned n) { (void)p;assert(n==2); }
void pio_gpio_init(PIO p,unsigned pin) { assert(p==pio1 && pin<3); }
int pio_sm_init(PIO p,unsigned n,unsigned off,const pio_sm_config *c)
{ assert(p==pio1 && n==2 && off==7 && c->out==0 && c->count==8 && !c->right && c->autopull && c->divider==256);return fail_resource==5?-1:0; }
void pio_sm_set_enabled(PIO p,unsigned n,bool on) { (void)p;assert(n==2);enabled=on; }
void pio_sm_clear_fifos(PIO p,unsigned n) { (void)p;(void)n;fifo_count=fifo_at=0; }
void pio_sm_restart(PIO p,unsigned n) { (void)p;(void)n; }
bool pio_sm_is_rx_fifo_empty(PIO p,unsigned n) { (void)p;(void)n;return fifo_at==fifo_count; }
uint32_t pio_sm_get(PIO p,unsigned n) { (void)p;(void)n;assert(fifo_at<fifo_count);return fifo[fifo_at++]; }
void pio_sm_exec(PIO p,unsigned n,unsigned instruction)
{
    (void)p;(void)n;
    if (instruction==0x3000) { if(fifo_at==fifo_count) fifo_at=fifo_count=0;assert(fifo_count<8);fifo[fifo_count++]=~sampled_bits; }
}
unsigned pio_get_dreq(PIO p,unsigned n,bool tx) { assert(p==pio1 && n==2 && !tx);return 6; }
int dma_claim_unused_channel(bool required) { assert(!required);return fail_resource==4?-1:3; }
void dma_channel_unclaim(unsigned n) { assert(n==3); }
void dma_channel_configure(unsigned n,const dma_channel_config *c,volatile void *dst,const void *src,uint32_t count,bool trigger)
{ assert(n==3 && c->size==DMA_SIZE_8 && !c->read && c->write && c->priority && c->dreq==6 && !trigger && count==4101 && src==&fake_pio.rxf[2]);destination=(uint8_t *)dst; }
void dma_channel_set_write_addr(unsigned n,void *dst,bool trigger) { assert(n==3 && !trigger);destination=dst; }
void dma_channel_set_trans_count(unsigned n,uint32_t count,bool trigger) { assert(n==3 && count==4101 && !trigger);fake_dma.ch[n].transfer_count=count; }
void dma_start_channel_mask(uint32_t mask) { assert(mask==8 && cs);++starts;fake_pio.fdebug=0; }
void hw_clear_bits(uint32_t *p,uint32_t bits) { assert(p==&fake_dma.ch[3].al1_ctrl);*p&=~bits; }
void hw_set_bits(uint32_t *p,uint32_t bits)
{ assert(p==&fake_dma.ch[3].al1_ctrl);*p|=bits&1;fake_dma.ch[3].ctrl_trig&=~(bits&0x60000000u); }
uint32_t save_and_disable_interrupts(void) { assert(!masked && !in_irq);masked=true;return 0; }
void restore_interrupts(uint32_t saved) { (void)saved;assert(masked);masked=false; }
void uart_puts(unsigned u,const char *s) { (void)u;(void)s;assert(!in_irq && !masked); }
static void edge(bool high,uint32_t flags)
{ cs=high;in_irq=true;callback(1,flags);in_irq=false; }
static void transfer(uint32_t counter,unsigned bytes,unsigned tail,unsigned bits,bool hw_fault)
{
    edge(false,GPIO_IRQ_EDGE_FALL);
    assert(enabled);
    unsigned n=bytes<4101?bytes:4101;
    memset(destination,0,n);
    for(unsigned i=0;i<4;++i) destination[i]=counter>>(8*i);
    fake_dma.ch[3].transfer_count=4101-n+tail;
    fifo_count=tail;fifo_at=0;for(unsigned i=0;i<tail;++i) fifo[i]=0;
    sampled_bits=bits;
    if(hw_fault) fake_dma.ch[3].ctrl_trig|=DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;
    edge(true,GPIO_IRQ_EDGE_RISE);
    fake_dma.ch[3].ctrl_trig=0;
}
int main(void)
{
    for(fail_resource=1;fail_resource<=5;++fail_resource) {
        rx_sm=rx_dma=rx_offset=-1;
        assert(!spi_audio_init() && spi_audio_error_count()==1);
    }
    fail_resource=0;assert(spi_audio_init());spi_audio_start();assert(armed && starts==1);
    transfer(0,4100,8,32800,false);transfer(1,4100,0,32800,false);
    assert(!spi_audio_error_count());const uint8_t *p=spi_audio_take();assert(p && p[0]==0);spi_audio_release();
    assert(spi_audio_take()[0]==1);spi_audio_release();
    for(unsigned kind=0;kind<5;++kind) {
        unsigned before=spi_audio_error_count();
        transfer(2+kind*3,kind==0?4099:kind==1?4101:4100,0,kind==2?32801:32800,kind==3);
        if(kind==4) { /* Valid preceding transfer, followed by missed CS edges. */
            edge(true,GPIO_IRQ_EDGE_RISE|GPIO_IRQ_EDGE_FALL);
        }
        assert(spi_audio_error_count()==before+1);
        assert(!spi_audio_take());
        transfer(3+kind*3,4100,0,32800,false);transfer(4+kind*3,4100,0,32800,false);
        assert(spi_audio_take());spi_audio_release();
        assert(spi_audio_take());spi_audio_release();
    }
    /* CS stuck low is bounded; rising CS recovers without a reset. */
    edge(false,GPIO_IRQ_EDGE_FALL);now+=9000;unsigned before=spi_audio_error_count();
    spi_audio_poll();assert(spi_audio_error_count()==before+1 && dropping && !enabled);
    edge(true,GPIO_IRQ_EDGE_RISE);assert(armed && spi_audio_error_count()==before+1);
    assert(!spi_audio_take());
    abort_stuck=true;transfer(40,4100,0,32800,false);
    assert(!armed && spi_audio_error_count()==before+2);
    abort_stuck=false;time_us_64();spi_audio_poll();assert(armed);
    assert(!spi_audio_take());
    transfer(41,4100,0,32800,false);transfer(42,4100,0,32800,false);
    assert(spi_audio_take()[0]==41);spi_audio_release();
    /* DMA unavailable at a falling edge must be counted, not silently lost. */
    armed=false;before=spi_audio_error_count();edge(false,GPIO_IRQ_EDGE_FALL);
    edge(true,GPIO_IRQ_EDGE_RISE);assert(spi_audio_error_count()==before+1 && armed);
    puts("SPI resource, CS, residual FIFO, DMA abort and recovery tests OK");
}
