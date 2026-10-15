#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "flash_update.h"
static uint8_t flash[0x200000], image_bytes[513], reply[FU_REPLY_SIZE], begin[FU_BEGIN_SIZE];
static fu_environment_t environment_state;
static unsigned writes,erases,buys,reboots;
static int fail_program;
static bool fail_erase, corrupt_program;
static bool layout_ok=true,quiesce_ok=true,image_ok=true;
static bool buy_ok, stale_boot_info, mutate_on_failed_buy;
static unsigned normal_reboots;
static void put(uint8_t *p,uint32_t x) { for(unsigned i=0;i<4;++i)p[i]=x>>(8*i); }
static uint32_t get(const uint8_t *p) { return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24; }
static void digest(const uint8_t *p,unsigned n,uint8_t *out) {
    memset(out,0,32); for(unsigned i=0;i<n;++i) out[i%32]=(out[i%32]*33)^p[i];
}
static bool environment(fu_environment_t *e) { *e=environment_state; return layout_ok; }
static bool maintenance(void) { return quiesce_ok; }
static bool read_flash(uint32_t off,uint8_t *p,uint32_t n) { assert(off+n<=sizeof(flash));memcpy(p,flash+off,n);return true; }
static void safe(uint32_t off,unsigned n) {
    uint32_t inactive=environment_state.boot ? FU_SLOT_A : FU_SLOT_B;
    assert((off>=inactive && off+n<=inactive+FU_SLOT_SIZE) || (off>=FU_META_A && off+n<=sizeof(flash)));
}
static bool erase(uint32_t off) { safe(off,4096);assert(off%4096==0);if(fail_erase)return false;memset(flash+off,255,4096);++erases;return true; }
static bool program(uint32_t off,const uint8_t *p) {
    safe(off,256); assert(off%256==0);++writes;
    if(fail_program>0 && --fail_program==0) { for(unsigned i=0;i<31;++i) flash[off+i]&=p[i];return false; }
    for(unsigned i=0;i<256;++i) flash[off+i]&=p[i];
    if(corrupt_program)flash[off]^=1;
    return true;
}
/* Model the ROM's sole supported image mutation, independently of boot_info.
 * Byte 263 is the IMAGE_TYPE high byte in our synthetic image. */
static bool hash(uint32_t off,uint32_t n,bool normalize,uint8_t *out) {
    uint8_t copy[sizeof(image_bytes)]; assert(n==sizeof(copy));
    memcpy(copy,flash+off,n); if(normalize) copy[263]|=0x80;
    digest(copy,n,out); return true;
}
static bool image(uint32_t off,uint32_t n,bool tbyb,uint16_t major,uint16_t minor,const uint8_t *id) {
    (void)n;return image_ok && !!(flash[off+263]&0x80)==tbyb && major==1 && minor==1 && id[0]==0x55;
}
static bool buy(void) {
    ++buys;
    /* A2-A4 clear recent_boot BEFORE error exits as well as success. */
    if(!stale_boot_info) environment_state.tbyb=0;
    if(buy_ok || mutate_on_failed_buy) flash[(environment_state.boot ? FU_SLOT_B : FU_SLOT_A)+263]&=0x7f;
    return buy_ok;
}
static bool reboot(uint32_t off) {
    if(off==FU_NORMAL_REBOOT) ++normal_reboots;
    else assert(off==(environment_state.boot ? FU_SLOT_A : FU_SLOT_B));
    ++reboots;return true;
}
static const fu_ops_t ops={environment,maintenance,read_flash,erase,program,hash,image,buy,reboot};
static void status(void) { uint8_t p=FU_STATUS;assert(!flash_update_execute(&p,1,reply)); }
static uint8_t command(uint8_t cmd) { uint8_t p[17];p[0]=cmd;memcpy(p+1,begin+1,16);return flash_update_execute(p,sizeof(p),reply); }
static uint8_t write_block(unsigned off,unsigned n,bool different) {
    uint8_t p[150]={FU_WRITE};memcpy(p+1,begin+1,16);put(p+17,off);p[21]=n;
    memcpy(p+22,image_bytes+off,n);if(different)p[22]^=1;
    return flash_update_execute(p,22+n,reply);
}
static void fresh(unsigned boot) {
    memset(flash,255,sizeof(flash)); memset(begin,0,sizeof(begin));
    environment_state=(fu_environment_t){.boot=boot,.valid=true,.ready=true,.major=1};
    layout_ok=quiesce_ok=image_ok=true;writes=erases=buys=reboots=0;fail_program=0;fail_erase=corrupt_program=false;
    buy_ok=true;stale_boot_info=mutate_on_failed_buy=false;normal_reboots=0;
    for(unsigned i=0;i<sizeof(image_bytes);++i)image_bytes[i]=(i*7)&255;
    image_bytes[263]|=0x80;
    begin[0]=FU_BEGIN;memset(begin+1,0x42,16);put(begin+17,sizeof(image_bytes));
    digest(image_bytes,sizeof(image_bytes),begin+21);memset(begin+53,0x55,16);
    begin[69]=1;begin[71]=1;begin[73]=FU_FORMAT;
    flash_update_init(&ops);
}
static void upload(void) {
    assert(!flash_update_execute(begin,sizeof(begin),reply));
    for(unsigned i=0;i<512;i+=128)assert(!write_block(i,128,false));
    assert(!write_block(512,1,false));assert(get(reply+12)==sizeof(image_bytes));
}
static void trial(void) {
    upload();assert(!command(FU_VERIFY));assert(!command(FU_ACTIVATE));
    flash_update_ack_consumed(FU_ACTIVATE);
    environment_state.boot^=1;environment_state.tbyb=1;environment_state.minor=1;
    memset(environment_state.build,0x55,16);flash_update_init(&ops);
}
int main(void) {
    for(unsigned boot=0;boot<2;++boot) {
        fresh(boot); assert(!flash_update_execute(begin,sizeof(begin),reply));
        unsigned saved_erases=erases;
        assert(!flash_update_execute(begin,sizeof(begin),reply) && erases==saved_erases);
        assert(!write_block(0,128,false));assert(get(reply+8)==128 && get(reply+12)==0);
        unsigned saved_writes=writes;assert(!write_block(0,128,false) && writes==saved_writes);
        assert(write_block(0,128,true));assert(write_block(256,128,false));
        assert(!write_block(128,128,false));assert(get(reply+12)==256);
        assert(write_block(0,128,true));assert(!write_block(0,128,false));
        flash_update_init(&ops);status();assert(reply[1]==FU_INTERRUPTED && get(reply+12)==0);
        upload();assert(!command(FU_VERIFY));assert(reply[1]==FU_VERIFIED);
        assert(command(FU_CONFIRM) && buys==0);
        assert(!command(FU_ACTIVATE));assert(reboots==0);
        flash_update_ack_consumed(FU_CONFIRM);assert(reboots==0);
        flash_update_ack_consumed(FU_ACTIVATE);assert(reboots==1);
        environment_state.boot^=1;environment_state.tbyb=1;environment_state.minor=1;memset(environment_state.build,0x55,16);
        flash_update_init(&ops);status();assert(reply[1]==FU_ACTIVATING);
        uint8_t wrong[17]={FU_CONFIRM};assert(flash_update_execute(wrong,sizeof(wrong),reply) && buys==0);
        assert(!command(FU_CONFIRM) && buys==1);assert(reply[1]==FU_CONFIRMED);
        assert(reply[4]==0 && reply[6] && reboots==1);
        /* Lost/partial ACK: STATUS cannot reset; matching CONFIRM can re-arm. */
        status();flash_update_ack_consumed(FU_STATUS);assert(reboots==1);
        assert(!command(FU_CONFIRM) && buys==1);
        flash_update_ack_consumed(FU_CONFIRM);assert(reboots==2 && normal_reboots==1);
        flash_update_init(&ops);status();assert(reply[1]==FU_CONFIRMED && !reply[6]);
        assert(!command(FU_CONFIRM));flash_update_ack_consumed(FU_CONFIRM);assert(reboots==2 && buys==1);

        /* Failure before audio stop, or after a partial DMA stop: no writes,
         * RF remains held, ABORT requests normal reset without flash access. */
        fresh(boot);quiesce_ok=false;
        assert(flash_update_execute(begin,sizeof(begin),reply));status();
        assert(reply[1]==FU_FAILED && reply[5]==FU_E_QUIESCE && reply[6]);
        assert(!writes && !erases && !reboots);
        assert(!command(FU_ABORT) && !writes && !erases && !reboots);
        status();assert(reply[1]==FU_ABORTED && reply[6]);
        flash_update_ack_consumed(FU_ABORT);assert(normal_reboots==1);
        flash_update_init(&ops);status();assert(reply[1]==FU_IDLE && !reply[6]);

        fresh(boot);upload();assert(!command(FU_ABORT));assert(!reboots);
        flash_update_ack_consumed(FU_ABORT);assert(normal_reboots==1);
        flash_update_init(&ops);status();assert(reply[1]==FU_ABORTED && !reply[6]);
        assert(!command(FU_ABORT));flash_update_ack_consumed(FU_ABORT);assert(reboots==1);
    }
    /* Success with stale boot_info is NOT the A2-A4 contract. Fail closed on
     * this injected inconsistency rather than weakening the checked contract. */
    fresh(0);trial();stale_boot_info=true;
    assert(command(FU_CONFIRM));status();assert(reply[1]==FU_FAILED && reply[5]==FU_E_ROM);
    flash_update_ack_consumed(FU_CONFIRM);assert(!normal_reboots);
    for(unsigned changed=0;changed<2;++changed) {
        fresh(0);trial();buy_ok=false;mutate_on_failed_buy=changed;
        assert(command(FU_CONFIRM) && buys==1);status();
        assert(reply[4]==0 && reply[1]==FU_FAILED && reply[5]==FU_E_ROM);
        assert(command(FU_CONFIRM) && buys==1);
        flash_update_ack_consumed(FU_CONFIRM);assert(!normal_reboots);
    }
    fresh(0);fail_erase=true;assert(flash_update_execute(begin,sizeof(begin),reply));assert(!writes);
    fresh(0);assert(!flash_update_execute(begin,sizeof(begin),reply));assert(!write_block(0,128,false));
    fail_program=1;assert(write_block(128,128,false));status();assert(get(reply+12)==0 && reply[1]==FU_FAILED);
    fresh(0);assert(!flash_update_execute(begin,sizeof(begin),reply));assert(!write_block(0,128,false));
    corrupt_program=true;assert(write_block(128,128,false));status();assert(get(reply+12)==0 && reply[1]==FU_FAILED);
    fresh(0);layout_ok=false;assert(flash_update_execute(begin,sizeof(begin),reply));assert(!writes && !erases);
    fresh(0);quiesce_ok=false;assert(flash_update_execute(begin,sizeof(begin),reply));assert(!writes && !erases);assert(flash_update_maintenance());
    fresh(0);put(begin+17,FU_SLOT_SIZE+1);assert(flash_update_execute(begin,sizeof(begin),reply));assert(!writes);
    fresh(0);upload();flash[FU_SLOT_B+128]^=1;assert(command(FU_VERIFY));assert(command(FU_ACTIVATE));assert(!reboots);
    fresh(0);upload();image_ok=false;assert(command(FU_VERIFY));assert(command(FU_ACTIVATE));
    fresh(0);upload();fail_program=2;assert(command(FU_VERIFY));flash_update_init(&ops);status();assert(reply[1]==FU_INTERRUPTED);
    fresh(0);upload();assert(!command(FU_VERIFY));flash_update_init(&ops);status();assert(reply[1]==FU_VERIFIED);
    assert(!command(FU_ACTIVATE));flash_update_init(&ops);status();assert(reply[2]==0 && reply[1]==FU_ACTIVATING);
    assert(command(FU_CONFIRM) && !buys);assert(!command(FU_ABORT));assert(reply[1]==FU_ABORTED);
    fresh(0);upload();assert(!command(FU_VERIFY));assert(!command(FU_ACTIVATE));
    environment_state.boot=1;environment_state.tbyb=1;environment_state.minor=1;memset(environment_state.build,0x55,16);
    flash_update_init(&ops);fail_program=1;
    assert(command(FU_CONFIRM) && buys==1 && environment_state.tbyb==0);
    flash_update_ack_consumed(FU_CONFIRM);assert(!normal_reboots);
    flash_update_init(&ops);status();assert(reply[1]==FU_CONFIRMED && reply[4]==0);
    assert(!command(FU_CONFIRM) && buys==1);flash_update_ack_consumed(FU_CONFIRM);assert(normal_reboots==1);
    flash_update_init(&ops);status();assert(reply[1]==FU_CONFIRMED && !reply[6]);
    puts("flash_update: boundaries, duplicate/lost ACK, power cuts, VERIFY, trial, rollback, CONFIRM passed");
}
