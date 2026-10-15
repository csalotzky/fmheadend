/* Reuse the existing fake I2C adapter to exercise the actual IRQ/STOP gate. */
#define main original_adapter_tests
#include "test_host_i2c.c"
#undef main
static unsigned reboot_calls;
static uint8_t subcommand;
static bool reject_command;
bool flash_update_maintenance(void) { return false; }
void flash_update_ack_consumed(uint8_t command) { foreground_only(); assert(command==subcommand); ++reboot_calls; }
uint8_t flash_update_execute(const uint8_t *p,size_t n,uint8_t *r) {
    foreground_only(); assert(n==17 && p[0]==subcommand);
    if(reject_command) return HOST_INTERNAL_ERROR;
    memset(r,0,FU_REPLY_SIZE); r[0]=1; r[1]=FU_ACTIVATING; return HOST_OK;
}
static void activate(void) {
    uint8_t request[20]={FU_COMMAND,255,17,FU_ACTIVATE};
    request[3]=subcommand;
    write_request(request,sizeof(request)); host_i2c_poll();
    assert(update_ack_armed); assert(read_byte()==HOST_OK); assert(read_byte()==128);
    finish_read(); host_i2c_poll(); assert(!reboot_calls);
}
int main(void) {
    for(subcommand=FU_ACTIVATE;subcommand<=FU_ABORT;++subcommand) {
    reboot_calls=0;uart_log[0]=0;
    fake_qn_reset(); host_i2c_init(255);
    activate();
    for(unsigned i=0;i<128;++i) read_byte();
    host_i2c_poll(); assert(!reboot_calls);
    read_crc(); host_i2c_poll(); assert(!reboot_calls);
    finish_read(); host_i2c_poll(); assert(reboot_calls==1);
    host_i2c_poll(); assert(reboot_calls==1);
    reboot_calls=0;
    activate();
    read_byte(); finish_read(); host_i2c_poll(); assert(!reboot_calls);
    uint8_t info[]={0x01,255,0};
    write_request(info,sizeof(info)); host_i2c_poll(); assert(!update_ack_armed && !reboot_calls);
    /* All bytes queued does not mean the last byte left the hardware FIFO. */
    activate();for(unsigned i=0;i<128;++i) read_byte();read_crc();
    test_i2c1.hw.txflr=1;finish_read();host_i2c_poll();assert(!reboot_calls);
    test_i2c1.hw.txflr=0;
    write_request(info,sizeof(info));host_i2c_poll();assert(!reboot_calls);
    activate();for(unsigned i=0;i<128;++i) read_byte();read_crc();
    irq(I2C_IC_INTR_STAT_R_STOP_DET_BITS|I2C_IC_INTR_STAT_R_TX_ABRT_BITS,0,0);
    host_i2c_poll();assert(!reboot_calls);
    /* Rejected CONFIRM/buy must never arm the normal reboot success gate. */
    reject_command=true;
    uint8_t request[20]={FU_COMMAND,255,17};request[3]=subcommand;
    write_request(request,sizeof(request));host_i2c_poll();assert(!update_ack_armed);
    reject_command=false;
    }
    puts("FLASH_UPDATE ACTIVATE/CONFIRM/ABORT: full ACK/CRC/STOP, FIFO, abort and replacement gate passed");
}
