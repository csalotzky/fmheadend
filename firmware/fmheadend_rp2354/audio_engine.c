#include "audio_engine.h"
#include "audio_samples.h"
#include "spi_audio.h"
#include "board.h"
#include "i2s_audio.pio.h"
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "hardware/uart.h"
#include <stdatomic.h>
#include <stdarg.h>
#include <stdio.h>


/* Core 0 only reads PIO/DMA for fault diagnostics after launch. No audio IRQ and no dependency on
 * QN bitbang/UART scheduling. Each buffer holds FRAMES_PER_PACKET active frames, total RAM 8 KiB.
 * Read rings are a containment measure for a missed rearm, NOT sample loops. */
static uint32_t buffers[2][AUDIO_BUFFER_CAPACITY_WORDS] __attribute__((aligned(4096)));
_Static_assert(sizeof(buffers[0])==4096, "DMA read ring must match buffer size");
_Static_assert(ATOMIC_INT_LOCK_FREE==2, "Cross-core audio handoff must be lock-free");
static atomic_uint state=AUDIO_STARTING, requested_mask;
static PIO pio=pio0;
static int sm=-1, channels[2]={-1,-1};
static uint32_t dma_mask, stall_bit;
#ifdef FMHEADEND_FLASH_UPDATE
#include "pico/flash.h"
static atomic_bool update_requested, update_ready, flash_ready;
static void update_stop(void) {
    if(!atomic_load(&update_requested)) return;
    bool ok=spi_audio_stop_for_update();
    pio_sm_set_enabled(pio0,sm,false);
    for(unsigned i=0;i<2;++i) hw_clear_bits(&dma_hw->ch[channels[i]].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_hw->abort=dma_mask;
    uint64_t until=time_us_64()+1000;
    while(dma_hw->abort & dma_mask) if(time_us_64()>until) { ok=false; break; }
    for(unsigned i=0;i<2;++i) if(dma_channel_is_busy(channels[i])) ok=false;
    pio_sm_set_pins_with_mask(pio0,sm,0,0x3ffu<<I2S_DATA_FIRST_PIN);
    atomic_store(&update_ready,ok);
    /* Permanent maintenance until reset. SDK lockout IRQ remains enabled. */
    while(true) tight_loop_contents();
}
#endif
/* Core 1 only publishes milestones and a pre-cleanup fault snapshot. UART is
 * exclusively on core 0: printing after DMA start must never delay refill. */
enum init_stage { CLOCK_CHECK, SM_CLAIM, PROGRAM_LOAD, DMA_CLAIM, SM_CONFIG, PIN_CONFIG,
    DMA_CONFIG, CORE_LAUNCH, BUFFER_PREPARE, FIFO_PRIME, PIO_START, REFILL };
static atomic_uint stage, milestones;
enum { CORE_READY=1, BUFFERS_READY=2, DMA_STARTED=4, FIFO_READY=8, PIO_STARTED=16 };
static uint64_t init_time;
static uint32_t system_hz, divider256;
static int program_offset=-1, sdk_result;
static unsigned prime_level;
static bool fault_logged;
typedef struct {
    unsigned stage;
    uint32_t elapsed_us, ctrl, pc, fstat, flevel, fdebug, shiftctrl, pinctrl;
    uint32_t dma_ctrl[2], remaining[2], read_addr[2], write_addr[2], completions, dma_intr;
    const char *condition;
} audio_snapshot_t;
static audio_snapshot_t fault_snapshot;
static atomic_bool snapshot_ready;
static unsigned completed_buffers; /* core 1 only */
static void audio_log(const char *format, ...)
{
    char line[256]; va_list args;
    va_start(args,format); vsnprintf(line,sizeof(line),format,args); va_end(args);
    uart_puts(DEBUG_UART,line);
}
static const char *stage_name(unsigned value)
{
    static const char *const names[]={"clock", "SM claim", "program load", "DMA claim",
        "SM config", "pin directions", "DMA config", "core1 launch/ready", "buffers", "FIFO prime",
        "PIO start", "refill"};
    return value<sizeof(names)/sizeof(names[0]) ? names[value] : "unknown";
}
static audio_snapshot_t snapshot(void)
{
    audio_snapshot_t s={0};
    s.stage=atomic_load(&stage); s.elapsed_us=(uint32_t)(time_us_64()-init_time);
    s.ctrl=pio->ctrl; s.fstat=pio->fstat; s.flevel=pio->flevel; s.fdebug=pio->fdebug;
    s.dma_intr=dma_hw->intr;
    if (sm>=0) {
        s.pc=pio->sm[sm].addr; s.shiftctrl=pio->sm[sm].shiftctrl; s.pinctrl=pio->sm[sm].pinctrl;
    }
    for (unsigned i=0;i<2;++i) if (channels[i]>=0) {
        s.dma_ctrl[i]=dma_hw->ch[channels[i]].ctrl_trig;
        s.remaining[i]=dma_hw->ch[channels[i]].transfer_count;
        s.read_addr[i]=dma_hw->ch[channels[i]].read_addr;
        s.write_addr[i]=dma_hw->ch[channels[i]].write_addr;
    }
    return s;
}
void audio_engine_log_fault(void)
{
    audio_state_t reason=audio_engine_state();
    if (reason<=AUDIO_RUNNING || fault_logged) return;
    fault_logged=true;
    audio_snapshot_t s=atomic_load_explicit(&snapshot_ready,memory_order_acquire) ? fault_snapshot : snapshot();
    audio_log("AUDIO: FAIL stage=%s reason=%s elapsed_us=%lu sdk_rc=%d\r\n",
        stage_name(s.stage),audio_engine_state_name(reason),(unsigned long)s.elapsed_us,sdk_result);
    audio_log("AUDIO: condition=%s DMA_INTR=%08lX\r\n",
        s.condition ? s.condition : (reason==AUDIO_DEADLINE_FAULT ? "core1 ready timeout" : "stage check"),
        (unsigned long)s.dma_intr);
    audio_log("AUDIO: resources PIO=0 SM=%d offset=%d DMA=%d,%d sys_hz=%lu div256=%lu milestones=0x%02X completed=%lu\r\n",
        sm,program_offset,channels[0],channels[1],(unsigned long)system_hz,(unsigned long)divider256,
        atomic_load(&milestones),(unsigned long)s.completions);
    audio_log("AUDIO: PIO CTRL=%08lX PC=%lu FSTAT=%08lX FLEVEL=%08lX FDEBUG=%08lX SHIFTCTRL=%08lX PINCTRL=%08lX\r\n",
        (unsigned long)s.ctrl,(unsigned long)s.pc,(unsigned long)s.fstat,(unsigned long)s.flevel,
        (unsigned long)s.fdebug,(unsigned long)s.shiftctrl,(unsigned long)s.pinctrl);
    for (unsigned i=0;i<2;++i) if (channels[i]>=0)
        audio_log("AUDIO: DMA%d CTRL=%08lX remaining=%lu read=%08lX write=%08lX\r\n",
            channels[i],(unsigned long)s.dma_ctrl[i],(unsigned long)s.remaining[i],
            (unsigned long)s.read_addr[i],(unsigned long)s.write_addr[i]);
}

audio_state_t audio_engine_state(void) { return atomic_load_explicit(&state,memory_order_acquire); }
void audio_engine_set_sources(uint8_t sine_mask, uint8_t digital_mask)
{ atomic_store_explicit(&requested_mask,(unsigned)sine_mask|((unsigned)digital_mask<<8),memory_order_release); }
const char *audio_engine_state_name(audio_state_t s)
{
    static const char *const names[]={"STARTING","RUNNING","RESOURCE FAULT","CLOCK FAULT",
        "DMA FAULT","PIO STALL","REFILL DEADLINE","PIO CONFIG FAULT"};
    return (unsigned)s<sizeof(names)/sizeof(names[0]) ? names[s] : "UNKNOWN FAULT";
}
static void fail(audio_state_t reason, const char *condition)
{
    fault_snapshot=snapshot(); fault_snapshot.completions=completed_buffers;
    fault_snapshot.condition=condition;
    atomic_store_explicit(&snapshot_ready,true,memory_order_release);
    pio_sm_set_enabled(pio,sm,false);
    for (unsigned i=0;i<2;++i) hw_clear_bits(&dma_hw->ch[channels[i]].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
    /* Stop pin activity even if hardware DMA abort itself fails. */
    pio_sm_set_pins_with_mask(pio,sm,0,0x3ffu<<I2S_DATA_FIRST_PIN);
    dma_hw->abort=dma_mask;
    atomic_store_explicit(&state,reason,memory_order_release);
}
static void core1_audio(void)
{
#ifdef FMHEADEND_FLASH_UPDATE
    atomic_store(&flash_ready,flash_safe_execute_core_init());
#endif
    spi_audio_start(); /* CS IRQ and queue consumption both belong to core 1. */
    atomic_fetch_or(&milestones,CORE_READY);
    atomic_store(&stage,BUFFER_PREPARE);
    unsigned phase=0, next=0;
    for (unsigned i=0;i<2;++i) audio_samples_fill(buffers[i],AUDIO_BUFFER_FRAMES,0,&phase);
    __dmb();
    atomic_fetch_or(&milestones,BUFFERS_READY);
    atomic_store(&stage,FIFO_PRIME);
    dma_start_channel_mask(1u<<channels[0]);
    atomic_fetch_or(&milestones,DMA_STARTED);
    uint64_t deadline=time_us_64()+1000;
    while (!pio_sm_is_tx_fifo_full(pio,sm)) {
        if (time_us_64()>deadline) { fail(AUDIO_DMA_FAULT,"TX FIFO prime timeout"); goto stopped; }
    }
    prime_level=pio_sm_get_tx_fifo_level(pio,sm);
    atomic_fetch_or(&milestones,FIFO_READY);
    atomic_store(&stage,PIO_START);
    pio->fdebug=stall_bit;
    pio_sm_set_enabled(pio,sm,true);
    atomic_fetch_or(&milestones,PIO_STARTED);
    unsigned expected=AUDIO_STARTING;
    if (!atomic_compare_exchange_strong(&state,&expected,AUDIO_RUNNING)) {
        fail(AUDIO_DEADLINE_FAULT,"startup publication timeout"); goto stopped;
    }
    atomic_store(&stage,REFILL);
    deadline=time_us_64()+((2u*FRAMES_PER_PACKET*1000000u+47999u)/48000u+666u);
    while (true) {
#ifdef FMHEADEND_FLASH_UPDATE
        update_stop();
#endif
        spi_audio_poll();
        uint32_t flags=dma_hw->intr & dma_mask;
        if (pio->fdebug & stall_bit) { fail(AUDIO_STALL_FAULT,"TXSTALL"); break; }
        if ((dma_hw->ch[channels[0]].ctrl_trig | dma_hw->ch[channels[1]].ctrl_trig) &
            DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS) { fail(AUDIO_DMA_FAULT,"DMA AHB error"); break; }
        if (flags) {
            unsigned ch=channels[next], other=channels[next^1u];
            /* Pause the free channel BEFORE writing its buffer. If already
             * retriggered it remains BUSY while paused: fail without writing. */
            hw_clear_bits(&dma_hw->ch[ch].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
            if (flags != (1u<<ch) || dma_channel_is_busy(ch) || !dma_channel_is_busy(other)) {
                fail(AUDIO_DEADLINE_FAULT,"DMA handoff flags/busy"); break;
            }
            dma_hw->intr=1u<<ch;
            ++completed_buffers;
            const uint8_t *pcm=spi_audio_take();
            unsigned sources=atomic_load_explicit(&requested_mask,memory_order_acquire);
            audio_samples_mix(buffers[next],AUDIO_BUFFER_FRAMES,sources&255,sources>>8,pcm,&phase);
            spi_audio_release();
            dma_channel_set_read_addr(ch,buffers[next],false);
            dma_channel_set_trans_count(ch,dma_encode_transfer_count(AUDIO_BUFFER_WORDS),false);
            __dmb();
            /* No transfer may have finished while its successor was paused. */
            if (!dma_channel_is_busy(other) || (dma_hw->intr & (1u<<other))) {
                fail(AUDIO_DEADLINE_FAULT,"peer completed during refill"); break;
            }
            /* CTRL_TRIG would START this channel now, concurrently with its
             * peer. Use the non-trigger alias; only CHAIN_TO may start it. */
            hw_set_bits(&dma_hw->ch[ch].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
            next^=1u;
            deadline=time_us_64()+((2u*FRAMES_PER_PACKET*1000000u+47999u)/48000u+666u);
        } else if (time_us_64()>deadline) { fail(AUDIO_DEADLINE_FAULT,"no DMA progress within two buffers plus guard"); break; }
        tight_loop_contents();
    }
stopped:
    /* Latched failure, reset required. No automatic clock restarts. */
    while (true) {
#ifdef FMHEADEND_FLASH_UPDATE
        update_stop();
#endif
        tight_loop_contents();
    }
}
bool audio_engine_enter_update(void) {
#ifdef FMHEADEND_FLASH_UPDATE
    if(!atomic_load(&flash_ready)) return false;
    atomic_store(&update_requested,true);
    uint64_t until=time_us_64()+100000;
    while(!atomic_load(&update_ready)) if(time_us_64()>until) return false;
    return true;
#else
    return false;
#endif
}
bool audio_engine_init(void)
{
    init_time=time_us_64();
    audio_log("AUDIO: init start\r\n");
    atomic_store(&stage,CLOCK_CHECK);
    system_hz=clock_get_hz(clk_sys);
    uint32_t div=divider256=audio_clock_divider256(system_hz);
    audio_log("AUDIO: clock sys_hz=%lu target_sm_hz=%u div256=%lu\r\n",
        (unsigned long)system_hz,AUDIO_SM_HZ,(unsigned long)div);
    if (!div) { atomic_store(&state,AUDIO_CLOCK_FAULT); audio_engine_log_fault(); return false; }
    atomic_store(&stage,SM_CLAIM);
    sm=pio_claim_unused_sm(pio,false);
    if (sm<0) goto resource_failure;
    audio_log("AUDIO: SM claimed PIO=0 SM=%d\r\n",sm);
    atomic_store(&stage,PROGRAM_LOAD);
    if (!pio_can_add_program(pio,&i2s_audio_program)) goto resource_failure;
    program_offset=pio_add_program(pio,&i2s_audio_program);
    if (program_offset<0) { sdk_result=program_offset; goto resource_failure; }
    audio_log("AUDIO: PIO program loaded offset=%d instructions=%u\r\n",program_offset,i2s_audio_program.length);
    atomic_store(&stage,DMA_CLAIM);
    channels[0]=dma_claim_unused_channel(false);
    channels[1]=dma_claim_unused_channel(false);
    if (channels[0]<0 || channels[1]<0) goto resource_failure;
    audio_log("AUDIO: DMA channels claimed A=%d B=%d\r\n",channels[0],channels[1]);
    unsigned offset=(unsigned)program_offset;
    atomic_store(&stage,SM_CONFIG);
    pio_sm_config cfg=i2s_audio_program_get_default_config(offset);
    sm_config_set_out_pins(&cfg,I2S_DATA_FIRST_PIN,8);
    sm_config_set_sideset_pins(&cfg,I2S_BCLK_PIN);
    sm_config_set_out_shift(&cfg,true,true,32);
    sm_config_set_fifo_join(&cfg,PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv_int_frac8(&cfg,div>>8,div&255);
    for (unsigned pin=I2S_DATA_FIRST_PIN;pin<=I2S_WS_PIN;++pin) pio_gpio_init(pio,pin);
    sdk_result=pio_sm_init(pio,sm,offset,&cfg);
    if (sdk_result) goto pio_failure;
    atomic_store(&stage,PIN_CONFIG);
    pio_sm_set_pins_with_mask(pio,sm,0,0x3ffu<<I2S_DATA_FIRST_PIN);
    sdk_result=pio_sm_set_consecutive_pindirs(pio,sm,I2S_DATA_FIRST_PIN,10,true);
    if (sdk_result) goto pio_failure;
    pio_sm_exec(pio,sm,pio_encode_set(pio_x,14));
    audio_log("AUDIO: SM configured DATA=%u..%u BCLK=%u WS=%u div=%lu+%lu/256 PC=%lu\r\n",
        I2S_DATA_FIRST_PIN,I2S_DATA_FIRST_PIN+7,I2S_BCLK_PIN,I2S_WS_PIN,
        (unsigned long)(div>>8),(unsigned long)(div&255),(unsigned long)pio->sm[sm].addr);
    stall_bit=1u<<(PIO_FDEBUG_TXSTALL_LSB+sm);
    dma_mask=(1u<<channels[0])|(1u<<channels[1]);
    atomic_store(&stage,DMA_CONFIG);
    for (unsigned i=0;i<2;++i) {
        dma_channel_config c=dma_channel_get_default_config(channels[i]);
        channel_config_set_transfer_data_size(&c,DMA_SIZE_32);
        channel_config_set_read_increment(&c,true);
        channel_config_set_write_increment(&c,false);
        channel_config_set_dreq(&c,pio_get_dreq(pio,sm,true));
        channel_config_set_chain_to(&c,channels[i^1u]);
        channel_config_set_ring(&c,false,12);
        channel_config_set_high_priority(&c,true);
        dma_channel_configure(channels[i],&c,&pio->txf[sm],buffers[i],
            dma_encode_transfer_count(AUDIO_BUFFER_WORDS),false);
    }
    audio_log("AUDIO: DMA configured words=%u ring_bytes=%u DREQ=%u A=%p B=%p\r\n",
        AUDIO_BUFFER_WORDS,(unsigned)sizeof(buffers[0]),pio_get_dreq(pio,sm,true),
        (void *)buffers[0],(void *)buffers[1]);
    dma_hw->intr=dma_mask;
    atomic_store(&stage,CORE_LAUNCH);
    audio_log("AUDIO: core1 launch\r\n");
    multicore_launch_core1(core1_audio);
    audio_log("AUDIO: core1 launched\r\n");
    /* Initialization on core 1 has a 1 ms FIFO priming deadline. */
    uint64_t deadline=time_us_64()+100000;
    while (audio_engine_state()==AUDIO_STARTING) {
        if (time_us_64()>deadline) {
            unsigned expected=AUDIO_STARTING;
            atomic_compare_exchange_strong(&state,&expected,AUDIO_DEADLINE_FAULT);
        }
        tight_loop_contents();
    }
    unsigned reached=atomic_load(&milestones);
    if (reached&CORE_READY) audio_log("AUDIO: core1 ready\r\n");
    if (reached&BUFFERS_READY) audio_log("AUDIO: buffers prepared frames=%u each; silence\r\n",AUDIO_BUFFER_FRAMES);
    if (reached&DMA_STARTED) audio_log("AUDIO: DMA started channel=%d\r\n",channels[0]);
    if (reached&FIFO_READY) audio_log("AUDIO: FIFO primed level=%u\r\n",prime_level);
    if (reached&PIO_STARTED) audio_log("AUDIO: PIO/DMA started SM=%d\r\n",sm);
    if (audio_engine_state()==AUDIO_RUNNING) { audio_log("AUDIO: init OK\r\n"); return true; }
    audio_engine_log_fault();
    return false;
pio_failure:
    atomic_store(&state,AUDIO_PIO_FAULT);
    goto release_resources;
resource_failure:
    atomic_store(&state,AUDIO_RESOURCE_FAULT);
release_resources:
    audio_engine_log_fault();
    for (unsigned i=0;i<2;++i) if (channels[i]>=0) dma_channel_unclaim(channels[i]);
    if (program_offset>=0) pio_remove_program(pio,&i2s_audio_program,program_offset);
    if (sm>=0) pio_sm_unclaim(pio,sm);
    return false;
}
