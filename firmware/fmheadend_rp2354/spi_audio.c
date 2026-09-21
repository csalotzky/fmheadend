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

/* Five immutable records per boot. Core 1 captures registers only; core 0
 * formats them later through the existing nonblocking UART queue. No waits,
 * extra FIFO reads, register clears or altered acceptance rules for tracing. */
#define RX_DIAGNOSTIC_LIMIT 5u
typedef struct {
    uint32_t remaining, write_offset, dma_ctrl, intr, fifo, fstat, fdebug, pc, cs;
} rx_registers_t;
typedef struct {
    rx_registers_t entry, stopped;
    uint32_t bytes, bits, dma_bytes, drained, fifo_after, shiftctrl, elapsed_us;
    bool aborted, fault, was_armed, was_active;
} rx_diagnostic_t;
static rx_diagnostic_t rx_diagnostics[RX_DIAGNOSTIC_LIMIT];
static atomic_uint rx_diagnostic_count;
static unsigned rx_diagnostic_read, rx_diagnostic_part; /* Core 0 only. */
static bool rx_diagnostic_limit_logged;
/* One accepted-frame accounting summary, independent of the five bad records. */
static struct { uint32_t dma_bytes, drained, remaining; } first_valid;
static atomic_bool first_valid_ready;
static bool first_valid_logged;

static rx_registers_t rx_registers(void)
{
    rx_registers_t r;
    /* Sequential MMIO observations, not a hardware-latched edge snapshot. */
    r.remaining=dma_hw->ch[rx_dma].transfer_count;
    r.write_offset=dma_hw->ch[rx_dma].write_addr-(uint32_t)(uintptr_t)spi_stream_capture(&stream);
    r.dma_ctrl=dma_hw->ch[rx_dma].ctrl_trig;
    r.intr=(dma_hw->intr>>rx_dma)&1u; /* Read only: may be latched from earlier RX. */
    r.fifo=pio_sm_get_rx_fifo_level(rx_pio,rx_sm);
    r.fstat=rx_pio->fstat;
    r.fdebug=rx_pio->fdebug;
    r.pc=rx_pio->sm[rx_sm].addr;
    r.cs=gpio_get(AUDIO_SPI_CS_PIN);
    return r;
}

static bool stop_dma(void)
{
    pio_sm_set_enabled(rx_pio,rx_sm,false);
    hw_clear_bits(&dma_hw->ch[rx_dma].al1_ctrl,DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_hw->abort=1u<<rx_dma;
    uint64_t end=time_us_64()+20;
    while ((dma_hw->abort&(1u<<rx_dma)) ||
           (dma_hw->ch[rx_dma].ctrl_trig&DMA_CH0_CTRL_TRIG_BUSY_BITS))
        if (time_us_64()>=end) return false;
    __dmb(); /* All outstanding SRAM writes completed before publishing a frame. */
    return true;
}
static void arm_capture(void)
{
    armed=false;
    if (!gpio_get(AUDIO_SPI_CS_PIN) || (dma_hw->abort&(1u<<rx_dma)) ||
        (dma_hw->ch[rx_dma].ctrl_trig&DMA_CH0_CTRL_TRIG_BUSY_BITS)) return;
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
    unsigned diagnostic_index=atomic_load_explicit(&rx_diagnostic_count,memory_order_relaxed);
    rx_diagnostic_t *diagnostic=diagnostic_index<RX_DIAGNOSTIC_LIMIT && !dropping ?
        &rx_diagnostics[diagnostic_index] : NULL;
    uint64_t diagnostic_start=0;
    if (diagnostic) {
        diagnostic->entry=rx_registers();
        diagnostic_start=time_us_64();
        diagnostic->was_armed=armed; diagnostic->was_active=active;
    }
    bool fault=!armed || !active;
    bool aborted=stop_dma();
    if (diagnostic) diagnostic->stopped=rx_registers();
    fault |= !aborted || (rx_pio->fdebug&stall_mask)!=0 ||
             (dma_hw->ch[rx_dma].ctrl_trig&DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS)!=0;
    uint32_t remaining=dma_hw->ch[rx_dma].transfer_count;
    uint8_t *buffer=spi_stream_capture(&stream);
    /* RP2350 ABORT forces TRANS_COUNT to zero; 4101-COUNT would invent bytes.
     * WRITE_ADDR advances only on completed writes. Once ABORT and BUSY clear,
     * its displacement counts every retired byte exactly once, including any
     * in-flight write. Only then may software consume the residual FIFO tail.
     * See RP2350 DMA CHn_WRITE_ADDR and erratum E5; no receive ring is enabled. */
    uint32_t dma_bytes=aborted ? dma_hw->ch[rx_dma].write_addr-(uint32_t)(uintptr_t)buffer : 0;
    fault |= dma_bytes>SPI_CAPTURE_BYTES;
    size_t count=dma_bytes<=SPI_CAPTURE_BYTES ? dma_bytes : 0;
    unsigned drained=0;
    /* At most eight residual FIFO entries; never CPU-receive the byte stream. */
    for (unsigned n=0;aborted && n<8 && !pio_sm_is_rx_fifo_empty(rx_pio,rx_sm);++n) {
        uint8_t byte=(uint8_t)pio_sm_get(rx_pio,rx_sm);
        if (count<SPI_CAPTURE_BYTES && aborted) buffer[count]=byte;
        ++count;
        ++drained;
    }
    if (diagnostic) diagnostic->fifo_after=pio_sm_get_rx_fifo_level(rx_pio,rx_sm);
    /* X was decremented once for EACH sampled bit, including partial bytes. */
    uint32_t bits=0;
    if (aborted) {
        pio_sm_exec(rx_pio,rx_sm,pio_encode_mov(pio_isr,pio_x));
        pio_sm_exec(rx_pio,rx_sm,pio_encode_push(false,false));
        bits=~pio_sm_get(rx_pio,rx_sm);
    }
    if (diagnostic && (fault || count!=SPI_FRAME_BYTES || bits!=8*SPI_FRAME_BYTES)) {
        diagnostic->bytes=count; diagnostic->bits=bits; diagnostic->drained=drained;
        diagnostic->dma_bytes=dma_bytes;
        diagnostic->aborted=aborted; diagnostic->fault=fault;
        diagnostic->shiftctrl=rx_pio->sm[rx_sm].shiftctrl;
        diagnostic->elapsed_us=(uint32_t)(time_us_64()-diagnostic_start);
        /* Never reuse a published slot, even after its UART output completes. */
        atomic_store_explicit(&rx_diagnostic_count,diagnostic_index+1,memory_order_release);
    }
    uint32_t previous_write=stream.write;
    if (!dropping) spi_stream_complete(&stream,count,bits,fault,time_us_64());
    if (stream.write!=previous_write && !atomic_load_explicit(&first_valid_ready,memory_order_relaxed)) {
        first_valid.dma_bytes=dma_bytes; first_valid.drained=drained; first_valid.remaining=remaining;
        atomic_store_explicit(&first_valid_ready,true,memory_order_release);
    }
    active=false; dropping=false; arm_capture();
}
bool spi_audio_init(void)
{
    char line[192];
    const char *stage="clock";
    int result=0;
    spi_stream_init(&stream);
    atomic_init(&rx_diagnostic_count,0);
    atomic_init(&first_valid_ready,false); first_valid_logged=false;
    rx_diagnostic_read=rx_diagnostic_part=0;
    rx_diagnostic_limit_logged=false;
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
    uart_puts(DEBUG_UART,"SPI RX DIAG: first 5 rejected length/hardware frames; entry -> stopped; details capped until reset\r\n");
    uart_puts(DEBUG_UART,"SPI RX EOF: retired WRITE_ADDR + FIFO tail; exact 4100 bytes / 32800 bits\r\n");
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
static bool rx_diagnostic_line(char *line, size_t capacity)
{
    if (!first_valid_logged && atomic_load_explicit(&first_valid_ready,memory_order_acquire)) {
        snprintf(line,capacity,"SPI RX FIRST OK: bytes=4100/4100 bits=32800/32800 dma_bytes=%lu drained=%lu rem_after_abort=%lu\r\n",
            (unsigned long)first_valid.dma_bytes,(unsigned long)first_valid.drained,(unsigned long)first_valid.remaining);
        first_valid_logged=true;
        return true;
    }
    unsigned available=atomic_load_explicit(&rx_diagnostic_count,memory_order_acquire);
    if (rx_diagnostic_read>=available) {
        if (available==RX_DIAGNOSTIC_LIMIT && !rx_diagnostic_limit_logged) {
            snprintf(line,capacity,"SPI RX DIAG: 5/5 captured; detailed capture/logging stopped until reset\r\n");
            rx_diagnostic_limit_logged=true;
            return true;
        }
        return false;
    }
    const rx_diagnostic_t *d=&rx_diagnostics[rx_diagnostic_read];
    const rx_registers_t *a=&d->entry, *b=&d->stopped;
    unsigned number=rx_diagnostic_read+1;
    if (rx_diagnostic_part==0) {
        snprintf(line,capacity,"SPI RX BAD #%u: bytes=%lu/4100 bits=%lu/32800 rem=%lu->%lu dma_bytes=%lu drained=%lu abort_ok=%u hw_fault=%u armed=%u active=%u cs=%lu->%lu\r\n",
            number,(unsigned long)d->bytes,(unsigned long)d->bits,
            (unsigned long)a->remaining,(unsigned long)b->remaining,(unsigned long)d->dma_bytes,(unsigned long)d->drained,
            d->aborted,d->fault,d->was_armed,d->was_active,(unsigned long)a->cs,(unsigned long)b->cs);
    } else if (rx_diagnostic_part==1) {
        bool progressed=b->write_offset>a->write_offset;
        bool pending=a->fifo || b->fifo || progressed;
        bool sampling=a->pc==(unsigned)rx_offset+2 || a->pc==(unsigned)rx_offset+3;
        snprintf(line,capacity,"SPI RX BAD #%u DMA: ctrl=%08lX->%08lX busy=%u->%u count_zero=%u->%u write_off=%lu->%lu irq_latched=%lu->%lu progressed=%u tail_pending=%u race_hint=%s\r\n",
            number,(unsigned long)a->dma_ctrl,(unsigned long)b->dma_ctrl,
            !!(a->dma_ctrl&DMA_CH0_CTRL_TRIG_BUSY_BITS),!!(b->dma_ctrl&DMA_CH0_CTRL_TRIG_BUSY_BITS),
            a->remaining==0,b->remaining==0,(unsigned long)a->write_offset,(unsigned long)b->write_offset,
            (unsigned long)a->intr,(unsigned long)b->intr,
            progressed,pending,pending||sampling ? "possible" : "not_observed");
    } else {
        snprintf(line,capacity,"SPI RX BAD #%u PIO: pc=%lu->%lu base=%d fifo=%lu->%lu->%lu fstat=%08lX->%08lX fdebug=%08lX->%08lX shift=%08lX end_us=%lu\r\n",
            number,(unsigned long)a->pc,(unsigned long)b->pc,rx_offset,
            (unsigned long)a->fifo,(unsigned long)b->fifo,(unsigned long)d->fifo_after,
            (unsigned long)a->fstat,(unsigned long)b->fstat,(unsigned long)a->fdebug,
            (unsigned long)b->fdebug,(unsigned long)d->shiftctrl,(unsigned long)d->elapsed_us);
    }
    if (++rx_diagnostic_part==3) { rx_diagnostic_part=0; ++rx_diagnostic_read; }
    return true;
}
bool spi_audio_diagnostic(char *line, size_t capacity, uint64_t now)
{
    if (rx_diagnostic_line(line,capacity)) return true;
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
