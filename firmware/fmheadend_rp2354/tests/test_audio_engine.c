/* Execute the production engine against deterministic DMA/PIO registers.
 * Real opcode/edge timing is tested separately by test_i2s_timing.py. */
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <string.h>
#include "../audio_engine.c"
fake_pio_t fake_pio;
fake_dma_t fake_dma;
static bool busy[2], enabled;
static unsigned claims, releases, starts, completions, current, scenario, pins;
static uint64_t now, next_complete;
static const uint32_t *read_address[2];
static jmp_buf done;
static unsigned rendered_phase;
enum { NORMAL, NO_SM, NO_PROGRAM, NO_DMA, BAD_CLOCK, NO_PRIME, STALL, DMA_ERROR,
       MISSED_BOTH, LATE_RETRIGGER, NO_PROGRESS, CORE_NOT_STARTED, LATE_REFILL };
uint32_t clock_get_hz(unsigned clk) { assert(clk==clk_sys);return scenario==BAD_CLOCK?125000000:150000000; }
int pio_claim_unused_sm(PIO p,bool required) { assert(p==pio0 && !required);return scenario==NO_SM?-1:0; }
bool pio_can_add_program(PIO p,const pio_program_t *prog) { (void)p;(void)prog;return scenario!=NO_PROGRAM; }
unsigned pio_add_program(PIO p,const pio_program_t *prog) { (void)p;(void)prog;return 0; }
void pio_sm_unclaim(PIO p,unsigned s) { (void)p;assert(s==0);++releases; }
int dma_claim_unused_channel(bool required) { assert(!required);return scenario==NO_DMA && claims==1?-1:(int)claims++; }
void dma_channel_unclaim(unsigned ch) { assert(ch<2);++releases; }
void pio_sm_init(PIO p,unsigned s,unsigned off,const pio_sm_config *c)
{
    (void)p;assert(s==0 && off==0 && c->out==4 && c->count==8 && c->side==12);
    assert(c->divider==6250 && c->right && c->autopull);
}
void pio_sm_exec(PIO p,unsigned s,unsigned instruction) { (void)p;(void)s;assert(instruction==46); }
void pio_sm_set_enabled(PIO p,unsigned s,bool value)
{
    (void)p;(void)s;enabled=value;
    if(value) { ++starts;fake_pio.fdebug=0;next_complete=now+2666; }
}
void pio_sm_set_pins_with_mask(PIO p,unsigned s,uint32_t value,uint32_t mask)
{ (void)p;(void)s;assert(value==0 && mask==(0x3ffu<<4)); }
void pio_sm_set_consecutive_pindirs(PIO p,unsigned s,unsigned first,unsigned count,bool output)
{ (void)p;(void)s;assert(first==4 && count==10 && output); }
void pio_gpio_init(PIO p,unsigned pin) { (void)p;assert(pin>=4 && pin<=13);pins|=1u<<pin; }
bool pio_sm_is_tx_fifo_full(PIO p,unsigned s) { (void)p;(void)s;return scenario!=NO_PRIME; }
unsigned pio_get_dreq(PIO p,unsigned s,bool tx) { (void)p;(void)s;assert(tx);return 7; }
void dma_channel_configure(unsigned ch,const dma_channel_config *c,volatile void *write,
                           const void *read,uint32_t count,bool trigger)
{
    assert(c->chain==(ch^1) && c->ring==12 && c->dreq==7 && c->size==DMA_SIZE_32);
    assert(c->read && !c->write && c->priority && !trigger && count==1024);
    assert(write==&fake_pio.txf[0] && ((uintptr_t)read&4095)==0);
    read_address[ch]=read;fake_dma.ch[ch].ctrl_trig=1;
}
void dma_start_channel_mask(uint32_t mask) { assert(mask==1);busy[0]=true; }
bool dma_channel_is_busy(unsigned ch) { return busy[ch]; }
void dma_channel_set_read_addr(unsigned ch,const void *read,bool trigger)
{
    assert(!trigger && !busy[ch] && !(fake_dma.ch[ch].ctrl_trig&1));
    read_address[ch]=read;
    fake_dma.intr=0; /* Model preceding write-one-to-clear acknowledgement. */
    if(scenario==LATE_REFILL) { busy[ch^1]=false;fake_dma.intr=1u<<(ch^1); }
}
void dma_channel_set_trans_count(unsigned ch,uint32_t count,bool trigger)
{ assert(!trigger && !busy[ch] && count==1024); }
uint64_t time_us_64(void) { now+=10;return now; }
void tight_loop_contents(void)
{
    if(audio_engine_state()>AUDIO_RUNNING) longjmp(done,1);
    if(!enabled || now<next_complete) return;
    if(scenario==STALL) { fake_pio.fdebug=stall_bit;return; }
    if(scenario==DMA_ERROR) { fake_dma.ch[0].ctrl_trig|=DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;return; }
    if(scenario==NO_PROGRESS) return;
    if(scenario==MISSED_BOTH) { fake_dma.intr=3;return; }
    if(scenario==LATE_RETRIGGER) { fake_dma.intr=1;busy[0]=true;return; }
    /* Buffers emitted in order. Initial two are silent; later masks come from
     * the fill two completions earlier. Decode against uninterrupted phase. */
    uint32_t expected[AUDIO_BUFFER_WORDS];
    uint8_t mask=completions<2?0:(uint8_t)(1u<<((completions-2)%8));
    audio_samples_fill(expected,AUDIO_BUFFER_FRAMES,mask,&rendered_phase);
    assert(!memcmp(expected,read_address[current],sizeof(expected)));
    assert(busy[current] && (fake_dma.ch[current].ctrl_trig&1));
    busy[current]=false;fake_dma.intr|=1u<<current;
    current^=1;busy[current]=true;
    audio_engine_set_sine_mask(1u<<(completions%8));
    ++completions;next_complete+=2667;
    if(completions==24) longjmp(done,1);
}
void multicore_launch_core1(void (*fn)(void))
{
    fake_dma.intr=0;
    if(scenario==CORE_NOT_STARTED) return;
    if(!setjmp(done)) fn();
}
static void reset(unsigned test)
{
    scenario=test;claims=releases=starts=completions=current=pins=rendered_phase=0;
    now=next_complete=0;enabled=false;memset(busy,0,sizeof(busy));
    memset(&fake_pio,0,sizeof(fake_pio));memset(&fake_dma,0,sizeof(fake_dma));
    sm=-1;channels[0]=channels[1]=-1;atomic_store(&state,AUDIO_STARTING);atomic_store(&requested_mask,0);
}
int main(void)
{
    reset(NORMAL); assert(audio_engine_init());
    assert(completions==24 && starts==1 && enabled && pins==(0x3ffu<<4));
    for(unsigned s=NO_SM;s<=LATE_REFILL;++s) {
        reset(s);
        /* Core-not-started timeout runs on core 0, without a core1 setjmp. */
        bool ok;
        if(s==CORE_NOT_STARTED) { if(!setjmp(done)) ok=audio_engine_init();else ok=false; }
        else ok=audio_engine_init();
        assert(!ok && audio_engine_state()>AUDIO_RUNNING && !enabled);
        if(s==NO_PROGRAM) assert(releases==1);
        if(s==NO_DMA) assert(releases==2);
        if(s>=NO_PRIME && s!=CORE_NOT_STARTED) assert(fake_dma.abort==3);
    }
    puts("Production DMA engine: startup, pins/divider, 24 continuous buffers, lane changes, resource/stall/deadline faults PASS");
}
