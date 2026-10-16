#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "flash_update.h"
static uint8_t flash[0x200000], image_bytes[4353], reply[FU_REPLY_SIZE], begin[FU_BEGIN_SIZE];
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
static bool inspect(uint32_t off,uint32_t n,uint16_t *major,uint16_t *minor,uint8_t *id) {
    (void)off;(void)n;*major=1;*minor=1;memset(id,0x55,16);return image_ok;
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
static const fu_ops_t ops={environment,maintenance,read_flash,erase,program,hash,image,inspect,buy,reboot};
static void status(void) { uint8_t p=FU_STATUS;assert(!flash_update_execute(&p,1,reply)); }
static uint8_t command(uint8_t cmd) { return flash_update_execute(&cmd,1,reply); }
static uint8_t write_block(unsigned off,unsigned n,bool different) {
    uint8_t p[255]={FU_WRITE};put(p+1,off);
    memcpy(p+5,image_bytes+off,n);if(different)p[5]^=1;
    return flash_update_execute(p,5+n,reply);
}
static void fresh(unsigned boot) {
    memset(flash,255,sizeof(flash)); memset(begin,0,sizeof(begin));
    environment_state=(fu_environment_t){.boot=boot,.valid=true,.ready=true,.major=1};
    layout_ok=quiesce_ok=image_ok=true;writes=erases=buys=reboots=0;fail_program=0;fail_erase=corrupt_program=false;
    buy_ok=true;stale_boot_info=mutate_on_failed_buy=false;normal_reboots=0;
    for(unsigned i=0;i<sizeof(image_bytes);++i)image_bytes[i]=(i*7)&255;
    image_bytes[263]|=0x80;
    begin[0]=FU_BEGIN;put(begin+1,sizeof(image_bytes));
    digest(image_bytes,sizeof(image_bytes),begin+5);
    flash_update_init(&ops);
}
static void upload(void) {
    assert(!flash_update_execute(begin,sizeof(begin),reply));
    for(unsigned i=0;i<sizeof(image_bytes);i+=FU_CHUNK) {
        unsigned n=sizeof(image_bytes)-i;if(n>FU_CHUNK)n=FU_CHUNK;
        assert(!write_block(i,n,false));
    }
    assert(get(reply+5)==sizeof(image_bytes));

}
static void trial(void) {
    upload();assert(!command(FU_VERIFY));assert(!command(FU_ACTIVATE));
    flash_update_ack_consumed(FU_ACTIVATE);
    environment_state.boot^=1;environment_state.tbyb=1;environment_state.minor=1;
    memset(environment_state.build,0x55,16);flash_update_init(&ops);
}
int main(void) {
    for(unsigned boot=0;boot<2;++boot) {
        fresh(boot);assert(!flash_update_execute(begin,sizeof(begin),reply));
        assert(!write_block(0,250,false));assert(get(reply+1)==250 && get(reply+5)==0);
        assert(!write_block(250,250,false));assert(get(reply+5)==256);
        unsigned saved=writes;assert(!write_block(250,250,false) && writes==saved);
        assert(write_block(250,250,true));
        /* Same BEGIN always resets RAM progress and replaces durable intent. */
        assert(!flash_update_execute(begin,sizeof(begin),reply));assert(get(reply+8)==0);
        assert(!write_block(0,250,false));flash_update_init(&ops);status();
        assert(reply[1]==FU_INTERRUPTED && get(reply+8)==0);
        upload();assert(!command(FU_VERIFY));assert(reply[1]==FU_VERIFIED);
        assert(!memcmp(flash+(boot ? FU_SLOT_A : FU_SLOT_B),image_bytes,sizeof(image_bytes)));
        assert(!command(FU_ACTIVATE));assert(!reboots);
        flash_update_ack_consumed(FU_STATUS);assert(!reboots);
        flash_update_ack_consumed(FU_ACTIVATE);assert(reboots==1);
        environment_state.boot^=1;environment_state.tbyb=1;environment_state.minor=1;
        memset(environment_state.build,0x55,16);flash_update_init(&ops);
        saved=erases;assert(flash_update_execute(begin,sizeof(begin),reply));assert(erases==saved);
        assert(!command(FU_CONFIRM) && buys==1 && normal_reboots==0);
        status();assert(reply[1]==FU_CONFIRMED && reply[4]==0);
        assert(!command(FU_CONFIRM) && buys==1);flash_update_ack_consumed(FU_CONFIRM);
        assert(normal_reboots==1);flash_update_init(&ops);status();assert(!reply[6]);
        /* A new upload of the same image remains a real write in either direction. */
        upload();assert(!command(FU_VERIFY));
        fresh(boot);quiesce_ok=false;assert(flash_update_execute(begin,sizeof(begin),reply));
        assert(!writes && !erases);quiesce_ok=true;upload();
    }
    fresh(0);trial();buy_ok=false;assert(command(FU_CONFIRM));
    assert(command(FU_CONFIRM) && buys==1);assert(!normal_reboots);
    fresh(0);upload();flash[FU_SLOT_B+128]^=1;assert(command(FU_VERIFY));assert(command(FU_ACTIVATE));
    fresh(0);upload();fail_program=2;assert(command(FU_VERIFY));flash_update_init(&ops);status();
    assert(reply[1]==FU_INTERRUPTED);upload();assert(!command(FU_VERIFY));
    fresh(0);environment_state.tbyb=1;assert(flash_update_execute(begin,sizeof(begin),reply));assert(!erases);
    puts("OTA: fresh restart, 250-byte page/sector crossing, active-slot protection, lost ACK and buy passed");
}
