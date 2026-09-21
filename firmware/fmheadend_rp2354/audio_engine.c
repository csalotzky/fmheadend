#include "audio_engine.h"
#include "audio_samples.h"
#include "board.h"
#include "i2s_audio.pio.h"
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include <stdatomic.h>

/* Core 0 never touches PIO/DMA after launch. No audio IRQ and no dependency on
 * QN bitbang/UART scheduling. Each buffer lasts 2.667 ms, total RAM 8 KiB.
 * Read rings are a containment measure for a missed rearm, NOT sample loops. */
static uint32_t buffers[2][AUDIO_BUFFER_WORDS] __attribute__((aligned(4096)));
_Static_assert(sizeof(buffers[0])==4096, "DMA read ring must match buffer size");
_Static_assert(ATOMIC_INT_LOCK_FREE==2, "Cross-core audio handoff must be lock-free");
static atomic_uint state=AUDIO_STARTING, requested_mask;
static PIO pio=pio0;
static int sm=-1, channels[2]={-1,-1};
static uint32_t dma_mask, stall_bit;

audio_state_t audio_engine_state(void) { return atomic_load_explicit(&state,memory_order_acquire); }
void audio_engine_set_sine_mask(uint8_t mask) { atomic_store_explicit(&requested_mask,mask,memory_order_release); }
const char *audio_engine_state_name(audio_state_t s)
{
    static const char *const names[]={"STARTING","RUNNING","RESOURCE FAULT","CLOCK FAULT",
        "DMA FAULT","PIO STALL","REFILL DEADLINE"};
    return (unsigned)s<sizeof(names)/sizeof(names[0]) ? names[s] : "UNKNOWN FAULT";
}
static void fail(audio_state_t reason)
{
    pio_sm_set_enabled(pio,sm,false);
    for (unsigned i=0;i<2;++i) hw_clear_bits(&dma_hw->ch[channels[i]].ctrl_trig,DMA_CH0_CTRL_TRIG_EN_BITS);
    /* Stop pin activity even if hardware DMA abort itself fails. */
    pio_sm_set_pins_with_mask(pio,sm,0,0x3ffu<<I2S_DATA_FIRST_PIN);
    dma_hw->abort=dma_mask;
    atomic_store_explicit(&state,reason,memory_order_release);
}
static void core1_audio(void)
{
    unsigned phase=0, next=0;
    for (unsigned i=0;i<2;++i) audio_samples_fill(buffers[i],AUDIO_BUFFER_FRAMES,0,&phase);
    __dmb();
    dma_start_channel_mask(1u<<channels[0]);
    uint64_t deadline=time_us_64()+1000;
    while (!pio_sm_is_tx_fifo_full(pio,sm)) {
        if (time_us_64()>deadline) { fail(AUDIO_DMA_FAULT); goto stopped; }
    }
    pio->fdebug=stall_bit;
    pio_sm_set_enabled(pio,sm,true);
    unsigned expected=AUDIO_STARTING;
    if (!atomic_compare_exchange_strong(&state,&expected,AUDIO_RUNNING)) {
        fail(AUDIO_DEADLINE_FAULT); goto stopped;
    }
    deadline=time_us_64()+6000;
    while (true) {
        uint32_t flags=dma_hw->intr & dma_mask;
        if (pio->fdebug & stall_bit) { fail(AUDIO_STALL_FAULT); break; }
        if ((dma_hw->ch[channels[0]].ctrl_trig | dma_hw->ch[channels[1]].ctrl_trig) &
            DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS) { fail(AUDIO_DMA_FAULT); break; }
        if (flags) {
            unsigned ch=channels[next], other=channels[next^1u];
            /* Pause the free channel BEFORE writing its buffer. If already
             * retriggered it remains BUSY while paused: fail without writing. */
            hw_clear_bits(&dma_hw->ch[ch].ctrl_trig,DMA_CH0_CTRL_TRIG_EN_BITS);
            if (flags != (1u<<ch) || dma_channel_is_busy(ch) || !dma_channel_is_busy(other)) {
                fail(AUDIO_DEADLINE_FAULT); break;
            }
            dma_hw->intr=1u<<ch;
            audio_samples_fill(buffers[next],AUDIO_BUFFER_FRAMES,
                atomic_load_explicit(&requested_mask,memory_order_acquire),&phase);
            dma_channel_set_read_addr(ch,buffers[next],false);
            dma_channel_set_trans_count(ch,dma_encode_transfer_count(AUDIO_BUFFER_WORDS),false);
            __dmb();
            /* No transfer may have finished while its successor was paused. */
            if (!dma_channel_is_busy(other) || (dma_hw->intr & (1u<<other))) {
                fail(AUDIO_DEADLINE_FAULT); break;
            }
            hw_set_bits(&dma_hw->ch[ch].ctrl_trig,DMA_CH0_CTRL_TRIG_EN_BITS);
            next^=1u;
            deadline=time_us_64()+6000;
        } else if (time_us_64()>deadline) { fail(AUDIO_DEADLINE_FAULT); break; }
        tight_loop_contents();
    }
stopped:
    /* Latched failure, reset required. No automatic clock restarts. */
    while (true) tight_loop_contents();
}
bool audio_engine_init(void)
{
    uint32_t div=audio_clock_divider256(clock_get_hz(clk_sys));
    if (!div) { atomic_store(&state,AUDIO_CLOCK_FAULT); return false; }
    sm=pio_claim_unused_sm(pio,false);
    if (sm<0) goto resource_failure;
    if (!pio_can_add_program(pio,&i2s_audio_program)) goto resource_failure;
    channels[0]=dma_claim_unused_channel(false);
    channels[1]=dma_claim_unused_channel(false);
    if (channels[0]<0 || channels[1]<0) goto resource_failure;
    unsigned offset=pio_add_program(pio,&i2s_audio_program);
    pio_sm_config cfg=i2s_audio_program_get_default_config(offset);
    sm_config_set_out_pins(&cfg,I2S_DATA_FIRST_PIN,8);
    sm_config_set_sideset_pins(&cfg,I2S_BCLK_PIN);
    sm_config_set_out_shift(&cfg,true,true,32);
    sm_config_set_fifo_join(&cfg,PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv_int_frac8(&cfg,div>>8,div&255);
    for (unsigned pin=I2S_DATA_FIRST_PIN;pin<=I2S_WS_PIN;++pin) pio_gpio_init(pio,pin);
    pio_sm_init(pio,sm,offset,&cfg);
    pio_sm_set_pins_with_mask(pio,sm,0,0x3ffu<<I2S_DATA_FIRST_PIN);
    pio_sm_set_consecutive_pindirs(pio,sm,I2S_DATA_FIRST_PIN,10,true);
    pio_sm_exec(pio,sm,pio_encode_set(pio_x,14));
    stall_bit=1u<<(PIO_FDEBUG_TXSTALL_LSB+sm);
    dma_mask=(1u<<channels[0])|(1u<<channels[1]);
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
    dma_hw->intr=dma_mask;
    multicore_launch_core1(core1_audio);
    /* Initialization on core 1 has a 1 ms FIFO priming deadline. */
    uint64_t deadline=time_us_64()+100000;
    while (audio_engine_state()==AUDIO_STARTING) {
        if (time_us_64()>deadline) {
            unsigned expected=AUDIO_STARTING;
            atomic_compare_exchange_strong(&state,&expected,AUDIO_DEADLINE_FAULT);
        }
        tight_loop_contents();
    }
    return audio_engine_state()==AUDIO_RUNNING;
resource_failure:
    for (unsigned i=0;i<2;++i) if (channels[i]>=0) dma_channel_unclaim(channels[i]);
    if (sm>=0) pio_sm_unclaim(pio,sm);
    atomic_store(&state,AUDIO_RESOURCE_FAULT);
    return false;
}
