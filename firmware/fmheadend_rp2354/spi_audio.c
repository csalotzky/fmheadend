#include "spi_audio.h"
#include "spi_stream.h"
#include "board.h"
#include "spi_audio.pio.h"
#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "hardware/uart.h"
#include <stdio.h>

/* PIO1 avoids disturbing the verified PIO0 I2S SM/program. PL022 SPI slave
 * needs clk_peri >= 12*SCK (RP2350 datasheet 12.3.5): unsuitable for 25 MHz.
 * Only core 1 touches the receiver after init. CS edges, not bytes, interrupt. */
static spi_stream_t stream;
static PIO rx_pio=pio1;
static int rx_sm=-1, rx_dma=-1, rx_offset=-1;
static bool initialized, armed, active, dropping;
static uint64_t selected_at;
static uint32_t stall_mask;

static bool stop_dma(void)
{
    pio_sm_set_enabled(rx_pio,rx_sm,false);
    hw_clear_bits(&dma_hw->ch[rx_dma].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_hw->abort=1u<<rx_dma;
    uint64_t end=time_us_64()+20;
    while (dma_hw->abort&(1u<<rx_dma)) if (time_us_64()>=end) return false;
    __dmb(); /* All outstanding SRAM writes completed before publishing a frame. */
    return true;
}
static void arm_capture(void)
{
    armed=false;
    if (!gpio_get(AUDIO_SPI_CS_PIN) || (dma_hw->abort&(1u<<rx_dma))) return;
    pio_sm_clear_fifos(rx_pio,rx_sm);
    pio_sm_restart(rx_pio,rx_sm);
    pio_sm_exec(rx_pio,rx_sm,pio_encode_mov_not(pio_x,pio_null));
    pio_sm_exec(rx_pio,rx_sm,pio_encode_jmp(rx_offset));
    rx_pio->fdebug=stall_mask;
    hw_set_bits(&dma_hw->ch[rx_dma].al1_ctrl,
        DMA_CH0_CTRL_TRIG_READ_ERROR_BITS|DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS);
    dma_channel_set_write_addr(rx_dma,spi_stream_capture(&stream),false);
    dma_channel_set_trans_count(rx_dma,dma_encode_transfer_count(SPI_CAPTURE_BYTES),false);
    hw_set_bits(&dma_hw->ch[rx_dma].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_start_channel_mask(1u<<rx_dma);
    pio_sm_set_enabled(rx_pio,rx_sm,true); /* Waits for next CS LOW in hardware. */
    armed=true;
}
static void cs_irq(uint pin, uint32_t events)
{
    if (pin!=AUDIO_SPI_CS_PIN) return;
    const uint32_t both=GPIO_IRQ_EDGE_RISE|GPIO_IRQ_EDGE_FALL;
    if ((events&both)==both) {
        /* CPU missed a boundary: never concatenate two transactions. */
        stop_dma(); spi_stream_error(&stream,SPI_RECEIVE_ERROR);
        stream.have_counter=false; active=false; dropping=false; arm_capture(); return;
    }
    if (events&GPIO_IRQ_EDGE_FALL) {
        selected_at=time_us_64(); active=true; dropping=!armed;
        if (dropping) { spi_stream_error(&stream,SPI_RECEIVE_ERROR); stream.have_counter=false; }
        return;
    }
    if (!(events&GPIO_IRQ_EDGE_RISE)) return;
    bool fault=!armed || !active;
    bool aborted=stop_dma();
    fault |= !aborted || (rx_pio->fdebug&stall_mask)!=0 ||
             (dma_hw->ch[rx_dma].ctrl_trig&DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS)!=0;
    uint32_t remaining=dma_hw->ch[rx_dma].transfer_count;
    fault |= remaining>SPI_CAPTURE_BYTES;
    size_t count=remaining<=SPI_CAPTURE_BYTES ? SPI_CAPTURE_BYTES-remaining : 0;
    uint8_t *buffer=spi_stream_capture(&stream);
    /* At most eight residual FIFO entries; never CPU-receive the byte stream. */
    for (unsigned n=0;aborted && n<8 && !pio_sm_is_rx_fifo_empty(rx_pio,rx_sm);++n) {
        uint8_t byte=(uint8_t)pio_sm_get(rx_pio,rx_sm);
        if (count<SPI_CAPTURE_BYTES && aborted) buffer[count]=byte;
        ++count;
    }
    /* X was decremented once for EACH sampled bit, including partial bytes. */
    uint32_t bits=0;
    if (aborted) {
        pio_sm_exec(rx_pio,rx_sm,pio_encode_mov(pio_isr,pio_x));
        pio_sm_exec(rx_pio,rx_sm,pio_encode_push(false,false));
        bits=~pio_sm_get(rx_pio,rx_sm);
    }
    if (!dropping) spi_stream_complete(&stream,count,bits,fault,time_us_64());
    active=false; dropping=false; arm_capture();
}
bool spi_audio_init(void)
{
    char line[192];
    const char *stage="clock";
    int result=0;
    spi_stream_init(&stream);
    /* Static PIO WAIT GPIO operands are the verified board pins. */
    _Static_assert(AUDIO_SPI_MOSI_PIN==0 && AUDIO_SPI_CS_PIN==1 && AUDIO_SPI_SCK_PIN==2,"SPI PIO pins");
    if (clock_get_hz(clk_sys)<150000000) goto failed;
    stage="PIO/SM claim";
    rx_sm=pio_claim_unused_sm(rx_pio,false);
    if (rx_sm<0 || !pio_can_add_program(rx_pio,&spi_audio_program)) goto failed;
    rx_offset=pio_add_program(rx_pio,&spi_audio_program);
    if (rx_offset<0) goto failed;
    stage="DMA claim";
    rx_dma=dma_claim_unused_channel(false);
    if (rx_dma<0) goto failed;
    for (unsigned pin=0;pin<=3;++pin) {
        gpio_init(pin); gpio_set_dir(pin,GPIO_IN);
    }
    gpio_pull_up(AUDIO_SPI_CS_PIN);
    for (unsigned pin=0;pin<=2;++pin) pio_gpio_init(rx_pio,pin);
    pio_sm_config cfg=spi_audio_program_get_default_config(rx_offset);
    sm_config_set_in_pins(&cfg,AUDIO_SPI_MOSI_PIN);
    sm_config_set_in_shift(&cfg,false,true,8); /* MSB first byte in low 8 bits. */
    sm_config_set_fifo_join(&cfg,PIO_FIFO_JOIN_RX);
    sm_config_set_clkdiv_int_frac8(&cfg,1,0);
    stage="SM configure";
    result=pio_sm_init(rx_pio,rx_sm,rx_offset,&cfg);
    if (result) goto failed;
    stall_mask=1u<<(PIO_FDEBUG_RXSTALL_LSB+rx_sm);
    dma_channel_config dma=dma_channel_get_default_config(rx_dma);
    channel_config_set_transfer_data_size(&dma,DMA_SIZE_8);
    channel_config_set_read_increment(&dma,false);
    channel_config_set_write_increment(&dma,true);
    channel_config_set_dreq(&dma,pio_get_dreq(rx_pio,rx_sm,false));
    channel_config_set_high_priority(&dma,true);
    dma_channel_configure(rx_dma,&dma,spi_stream_capture(&stream),&rx_pio->rxf[rx_sm],
        dma_encode_transfer_count(SPI_CAPTURE_BYTES),false);
    initialized=true;
    snprintf(line,sizeof(line),"SPI AUDIO: init OK PIO1 SM=%d DMA=%d MOSI=0 CS=1 SCK=2 MISO=unused; mode0 25MHz; slots=4 queued=3 prefill=2 frame=4100\r\n",rx_sm,rx_dma);
    uart_puts(DEBUG_UART,line);
    return true;
failed:
    spi_stream_error(&stream,SPI_RECEIVE_ERROR);
    snprintf(line,sizeof(line),"SPI AUDIO: INIT FAILED stage=%s rc=%d sys_hz=%lu SM=%d offset=%d DMA=%d; DIGITAL silence\r\n",
        stage,result,(unsigned long)clock_get_hz(clk_sys),rx_sm,rx_offset,rx_dma);
    uart_puts(DEBUG_UART,line);
    if (rx_dma>=0) dma_channel_unclaim(rx_dma);
    if (rx_offset>=0) pio_remove_program(rx_pio,&spi_audio_program,rx_offset);
    if (rx_sm>=0) pio_sm_unclaim(rx_pio,rx_sm);
    return false;
}
void spi_audio_start(void)
{
    if (!initialized) return;
    gpio_acknowledge_irq(AUDIO_SPI_CS_PIN,GPIO_IRQ_EDGE_RISE|GPIO_IRQ_EDGE_FALL);
    arm_capture();
    gpio_set_irq_enabled_with_callback(AUDIO_SPI_CS_PIN,GPIO_IRQ_EDGE_RISE|GPIO_IRQ_EDGE_FALL,true,cs_irq);
}
void spi_audio_poll(void)
{
    if (!initialized) return;
    uint32_t saved=save_and_disable_interrupts();
    if (active && !dropping && (time_us_64()-selected_at>8000 ||
        (dma_hw->ch[rx_dma].ctrl_trig&DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS))) {
        stop_dma(); armed=false; dropping=true; stream.have_counter=false;
        spi_stream_error(&stream,SPI_RECEIVE_ERROR);
    }
    /* Handles a transaction already active when firmware started, or a delayed
     * abort. Resume only at CS HIGH, never halfway through the next frame. */
    if (!armed && !active && gpio_get(AUDIO_SPI_CS_PIN)) arm_capture();
    restore_interrupts(saved);
}
const uint8_t *spi_audio_take(void)
{
    uint32_t saved=save_and_disable_interrupts();
    const uint8_t *frame=spi_stream_take(&stream,time_us_64());
    restore_interrupts(saved); return frame;
}
void spi_audio_release(void)
{
    uint32_t saved=save_and_disable_interrupts();
    spi_stream_release(&stream); restore_interrupts(saved);
}
uint32_t spi_audio_error_count(void) { return spi_stream_errors(&stream); }
bool spi_audio_diagnostic(char *line, size_t capacity, uint64_t now)
{
    static uint64_t next_log;
    if (now<next_log || !atomic_load(&stream.events)) return false;
    unsigned events=atomic_exchange(&stream.events,0);
    next_log=now+1000000;
    snprintf(line,capacity,"SPI AUDIO:%s%s%s%s%s%s%s%s%s errors=%lu\r\n",
        events&SPI_FIRST?" first valid frame":"",events&SPI_LOST?" stream lost":"",
        events&SPI_RECOVERED?" stream recovered":"",events&SPI_LENGTH?" invalid length":"",
        events&SPI_GAP?" counter gap":"",events&SPI_DUPLICATE?" duplicate":"",
        events&SPI_BACKWARD?" backward counter":"",events&SPI_RECEIVE_ERROR?" receive/DMA error":"",
        events&SPI_QUEUE_FULL?" queue overflow":"",(unsigned long)spi_audio_error_count());
    return true;
}
