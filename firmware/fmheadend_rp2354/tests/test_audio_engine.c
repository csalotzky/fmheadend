/* Execute the production engine against deterministic DMA/PIO registers.
 * Real opcode/edge timing is tested separately by test_i2s_timing.py. */
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <string.h>
#define FMHEADEND_FLASH_UPDATE 1
#include "../audio_engine.c"
fake_pio_t fake_pio;
fake_dma_t fake_dma;
static bool busy[2], enabled;
static unsigned claims, releases, starts, completions, current, scenario, pins;
static uint64_t now, next_complete;
static const uint32_t *read_address[2];
static jmp_buf done;
static unsigned rendered_phase;
static bool on_core1;
static unsigned trigger_writes, programs_removed;
static char uart_text[8192];
enum { NORMAL, NO_SM, NO_PROGRAM, NO_DMA, BAD_CLOCK, NO_PRIME, STALL, DMA_ERROR,
       MISSED_BOTH, LATE_RETRIGGER, NO_PROGRESS, CORE_NOT_STARTED, LATE_REFILL,
       PROGRAM_LOAD_ERROR, SM_CONFIG_ERROR, PINDIR_ERROR, SPI_NORMAL };
static uint8_t pcm[4100];
static bool borrowed;
static bool testing_stop, stop_stuck, spi_stop_ok=true;
bool flash_safe_execute_core_init(void) { return true; }
bool spi_audio_stop_for_update(void) { assert(on_core1);return spi_stop_ok; }
void spi_audio_start(void) { assert(on_core1); }
void spi_audio_poll(void) {}
const uint8_t *spi_audio_take(void) {
    assert(!borrowed);borrowed=true;
    memset(pcm,(uint8_t)completions,sizeof(pcm));
    return scenario==SPI_NORMAL && completions%5 ? pcm : NULL;
}
void spi_audio_release(void) { assert(borrowed);borrowed=false; }
void uart_puts(unsigned uart,const char *text)
{
    assert(uart==uart0 && !on_core1); /* Logging must never cause a refill miss. */
    assert(strlen(uart_text)+strlen(text)<sizeof(uart_text));
    strcat(uart_text,text);
}
static void control_write(uint32_t *p,uint32_t value)
{
    for(unsigned ch=0;ch<2;++ch) {
        bool trigger=p==&fake_dma.ch[ch].ctrl_trig;
        if (trigger || p==&fake_dma.ch[ch].al1_ctrl) {
            fake_dma.ch[ch].ctrl_trig=fake_dma.ch[ch].al1_ctrl=value;
            if (trigger && (value&1) && !busy[ch]) { busy[ch]=true; ++trigger_writes; }
            return;
        }
    }
    assert(false);
}
void hw_clear_bits(uint32_t *p,uint32_t bits) { control_write(p,*p&~bits); }
void hw_set_bits(uint32_t *p,uint32_t bits) { control_write(p,*p|bits); }
uint32_t clock_get_hz(unsigned clk) { assert(clk==clk_sys);return scenario==BAD_CLOCK?125000000:150000000; }
int pio_claim_unused_sm(PIO p,bool required) { assert(p==pio0 && !required);return scenario==NO_SM?-1:0; }
bool pio_can_add_program(PIO p,const pio_program_t *prog) { (void)p;(void)prog;return scenario!=NO_PROGRAM; }
int pio_add_program(PIO p,const pio_program_t *prog) { (void)p;(void)prog;return scenario==PROGRAM_LOAD_ERROR?-1:24; }
void pio_remove_program(PIO p,const pio_program_t *prog,unsigned offset)
{ (void)p;(void)prog;assert(offset==24);++programs_removed; }
void pio_sm_unclaim(PIO p,unsigned s) { (void)p;assert(s==0);++releases; }
int dma_claim_unused_channel(bool required) { assert(!required);return scenario==NO_DMA && claims==1?-1:(int)claims++; }
void dma_channel_unclaim(unsigned ch) { assert(ch<2);++releases; }
int pio_sm_init(PIO p,unsigned s,unsigned off,const pio_sm_config *c)
{
    assert(s==0 && off==24 && c->out==4 && c->count==8 && c->side==12);
    assert(c->divider==6250 && c->right && c->autopull);
    p->sm[s].addr=off;
    return scenario==SM_CONFIG_ERROR?-2:0;
}
void pio_sm_exec(PIO p,unsigned s,unsigned instruction) { (void)p;(void)s;assert(instruction==46); }
void pio_sm_set_enabled(PIO p,unsigned s,bool value)
{
    (void)s;enabled=value;p->ctrl=value;
    if(value) { ++starts;fake_pio.fdebug=0;next_complete=now+2666; }
}
void pio_sm_set_pins_with_mask(PIO p,unsigned s,uint32_t value,uint32_t mask)
{ (void)p;(void)s;assert(value==0 && mask==(0x3ffu<<4)); }
int pio_sm_set_consecutive_pindirs(PIO p,unsigned s,unsigned first,unsigned count,bool output)
{ (void)p;(void)s;assert(first==4 && count==10 && output); return scenario==PINDIR_ERROR?-3:0; }
void pio_gpio_init(PIO p,unsigned pin) { (void)p;assert(pin>=4 && pin<=13);pins|=1u<<pin; }
bool pio_sm_is_tx_fifo_full(PIO p,unsigned s)
{
    (void)p;(void)s;
    assert(!enabled && busy[0] && !busy[1]); /* DMA must prime BEFORE enabling PIO. */
    assert((atomic_load(&milestones)&(CORE_READY|BUFFERS_READY|DMA_STARTED))==7);
    for(unsigned i=0;i<2;++i) for(unsigned w=0;w<AUDIO_BUFFER_WORDS;++w)
        assert(read_address[i][w]==0);
    return scenario!=NO_PRIME;
}
unsigned pio_sm_get_tx_fifo_level(PIO p,unsigned s) { (void)p;(void)s;return scenario==NO_PRIME?0:8; }
unsigned pio_get_dreq(PIO p,unsigned s,bool tx) { (void)p;(void)s;assert(tx);return 7; }
void dma_channel_configure(unsigned ch,const dma_channel_config *c,volatile void *write,
                           const void *read,uint32_t count,bool trigger)
{
    assert(c->chain==(ch^1) && c->ring==12 && c->dreq==7 && c->size==DMA_SIZE_32);
    assert(c->read && !c->write && c->priority && !trigger && count==1024);
    assert(write==&fake_pio.txf[0] && ((uintptr_t)read&4095)==0);
    read_address[ch]=read;fake_dma.ch[ch].ctrl_trig=fake_dma.ch[ch].al1_ctrl=1;
    fake_dma.ch[ch].transfer_count=count;
    fake_dma.ch[ch].read_addr=(uintptr_t)read;fake_dma.ch[ch].write_addr=(uintptr_t)write;
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
uint64_t time_us_64(void) {
    now+=10;
    if(testing_stop && !stop_stuck && fake_dma.abort) {
        fake_dma.abort=0;busy[0]=busy[1]=false;
    }
    return now;
}
void tight_loop_contents(void)
{
    if(testing_stop) longjmp(done,1); /* Observe the permanent core1 park. */
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
    if (scenario==SPI_NORMAL && completions>=2) {
        uint8_t expected_pcm[4100];memset(expected_pcm,(uint8_t)(completions-1),sizeof(expected_pcm));
        audio_samples_mix(expected,AUDIO_BUFFER_FRAMES,mask,255,
            (completions-1)%5 ? expected_pcm : NULL,&rendered_phase);
    } else audio_samples_fill(expected,AUDIO_BUFFER_FRAMES,mask,&rendered_phase);
    assert(!memcmp(expected,read_address[current],sizeof(expected)));
    assert(busy[current] && (fake_dma.ch[current].ctrl_trig&1));
    busy[current]=false;fake_dma.intr|=1u<<current;
    current^=1;busy[current]=true;
    audio_engine_set_sources(1u<<(completions%8),scenario==SPI_NORMAL?255:0);
    ++completions;next_complete+=2667;
    if(completions==24) longjmp(done,1);
}
void multicore_launch_core1(void (*fn)(void))
{
    assert(atomic_load(&stage)==CORE_LAUNCH);
    assert(strstr(uart_text,"SM configured") && strstr(uart_text,"DMA configured"));
    fake_dma.intr=0;
    if(scenario==CORE_NOT_STARTED) return;
    on_core1=true;
    if(!setjmp(done)) fn();
    on_core1=false;
}
static void reset(unsigned test)
{
    atomic_store(&update_requested,false);atomic_store(&update_ready,false);atomic_store(&flash_ready,false);
    testing_stop=stop_stuck=false;spi_stop_ok=true;
    scenario=test;claims=releases=starts=completions=current=pins=rendered_phase=0;
    now=next_complete=0;enabled=false;memset(busy,0,sizeof(busy));
    memset(&fake_pio,0,sizeof(fake_pio));memset(&fake_dma,0,sizeof(fake_dma));
    sm=-1;channels[0]=channels[1]=-1;atomic_store(&state,AUDIO_STARTING);atomic_store(&requested_mask,0);
    atomic_store(&stage,CLOCK_CHECK);atomic_store(&milestones,0);atomic_store(&snapshot_ready,false);
    program_offset=-1;sdk_result=0;completed_buffers=0;fault_logged=false;
    borrowed=false;uart_text[0]=0;on_core1=false;trigger_writes=programs_removed=0;
}
int main(void)
{
    /* Model the hardware effect that the original mock omitted: CTRL_TRIG
     * starts an enabled idle channel; AL1_CTRL only arms it for chaining. */
    reset(NORMAL); busy[1]=true;
    hw_set_bits(&fake_dma.ch[0].ctrl_trig,1);
    assert(busy[0] && busy[1] && trigger_writes==1);
    reset(NORMAL); busy[1]=true;
    hw_set_bits(&fake_dma.ch[0].al1_ctrl,1);
    assert(!busy[0] && busy[1] && trigger_writes==0);
    reset(NORMAL); assert(audio_engine_init());
    assert(completions==24 && starts==1 && enabled && pins==(0x3ffu<<4));
    assert(trigger_writes==0);
    assert(strstr(uart_text,"AUDIO: init start") && strstr(uart_text,"AUDIO: init OK"));
    assert(strstr(uart_text,"PIO program loaded offset=24") && strstr(uart_text,"FIFO primed level=8"));
    assert(strstr(uart_text,"core1 ready") && strstr(uart_text,"buffers prepared"));
    assert(!strstr(uart_text,"FAIL"));
    reset(SPI_NORMAL);assert(audio_engine_init());
    assert(completions==24 && starts==1 && enabled && trigger_writes==0);
    for(unsigned s=NO_SM;s<=PINDIR_ERROR;++s) {
        reset(s);
        /* Core-not-started timeout runs on core 0, without a core1 setjmp. */
        bool ok;
        if(s==CORE_NOT_STARTED) { if(!setjmp(done)) ok=audio_engine_init();else ok=false; }
        else ok=audio_engine_init();
        audio_engine_log_fault();
        assert(!ok && audio_engine_state()>AUDIO_RUNNING && !enabled);
        assert(strstr(uart_text,"AUDIO: FAIL stage=") && strstr(uart_text,"sys_hz="));
        assert(!strstr(uart_text,"init OK"));
        if(s==NO_PROGRAM) assert(releases==1);
        if(s==NO_DMA) assert(releases==2);
        if(s>=NO_PRIME && s<=LATE_REFILL && s!=CORE_NOT_STARTED) assert(fake_dma.abort==3);
        if(s==NO_PRIME) assert(strstr(uart_text,"stage=FIFO prime") && !(atomic_load(&milestones)&PIO_STARTED));
        if(s==STALL) assert(strstr(uart_text,"stage=refill") && strstr(uart_text,"FDEBUG=01000000"));
        if(s==BAD_CLOCK) assert(strstr(uart_text,"stage=clock") && strstr(uart_text,"sys_hz=125000000 div256=0"));
        if(s==SM_CONFIG_ERROR || s==PINDIR_ERROR) assert(audio_engine_state()==AUDIO_PIO_FAULT && programs_removed==1);
        size_t size=strlen(uart_text);audio_engine_log_fault();assert(strlen(uart_text)==size);
    }
    for(unsigned failure=0;failure<3;++failure) {
        reset(NORMAL);assert(audio_engine_init());
        testing_stop=true;stop_stuck=failure==1;spi_stop_ok=failure!=2;
        atomic_store(&update_requested,true);on_core1=true;
        if(!setjmp(done)) update_stop();
        on_core1=false;
        assert(!enabled && !(fake_dma.ch[0].al1_ctrl&1) && !(fake_dma.ch[1].al1_ctrl&1));
        assert(atomic_load(&update_ready)==(failure==0));
        assert(audio_engine_enter_update()==(failure==0));
    }
    reset(NORMAL);assert(!audio_engine_enter_update());assert(!atomic_load(&update_requested));
    puts("Production DMA engine: startup, continuous buffers, faults, update stop/partial stop/lockout readiness PASS");
}
