#include "flash_update.h"
#include "host_protocol.h"
#include "audio_engine.h"
#include "qn_boot.h"
#include "update_build.h"
#include "pico/bootrom.h"
#include "pico/flash.h"
#include "pico/sha256.h"
#include "boot/picobin.h"
#include "boot/picoboot_constants.h"
#include "hardware/dma.h"
#include "hardware/flash.h"
#include <string.h>

static uint32_t scratch[1024];
static bool initialized, quiet;
/* Ordinary application data in the native SDK .bin, also checked by the packager. */
static const volatile struct {
    char magic[16]; uint8_t id[16]; char version[16]; uint16_t major,minor;
} identity={"FMHEADEND-OTA-V1",{FMHEADEND_BUILD_BYTES},HOST_FIRMWARE_VERSION,
            PICO_CRT0_VERSION_MAJOR,PICO_CRT0_VERSION_MINOR};
static bool environment(fu_environment_t *e) {
    memset(e,0,sizeof(*e)); e->boot=255; e->tbyb=255;
    e->ready=initialized; for(unsigned j=0;j<16;++j) { e->build[j]=identity.id[j]; e->version[j]=identity.version[j]; }
    e->major=identity.major; e->minor=identity.minor;
    boot_info_t boot;
    uint32_t pt[4],p[3];
    if(!rom_get_boot_info(&boot) || boot.partition<0 || boot.partition>1 ||
       rom_get_partition_table_info(pt,4,PT_INFO_PT_INFO)!=4 || pt[0]!=PT_INFO_PT_INFO ||
       (pt[1]&0x1ffu)!=0x102u || rom_get_b_partition(0)!=1) return false;
    for(unsigned i=0;i<2;++i) {
        if(rom_get_partition_table_info(p,3,(i<<24)|PT_INFO_SINGLE_PARTITION|PT_INFO_PARTITION_LOCATION_AND_FLAGS)!=3 ||
           (p[0]&PT_INFO_PARTITION_LOCATION_AND_FLAGS)==0) return false;
        uint32_t first=(p[1]&PICOBIN_PARTITION_LOCATION_FIRST_SECTOR_BITS)*4096u;
        uint32_t end=(((p[1]&PICOBIN_PARTITION_LOCATION_LAST_SECTOR_BITS)>>PICOBIN_PARTITION_LOCATION_LAST_SECTOR_LSB)+1)*4096u;
        if(first!=(i ? FU_SLOT_B : FU_SLOT_A) || end-first!=FU_SLOT_SIZE ||
           !(p[1]&PICOBIN_PARTITION_PERMISSION_S_W_BITS) ||
           !(p[2]&PICOBIN_PARTITION_FLAGS_ACCEPTS_DEFAULT_FAMILY_RP2350_ARM_S_BITS)) return false;
    }
    uint32_t current=boot.partition ? FU_SLOT_B : FU_SLOT_A;
    /* ROM must actually map this build's runtime origin to the reported slot. */
    if(rom_flash_runtime_to_storage_addr(XIP_BASE)!=(intptr_t)(XIP_BASE+current) ||
       rom_flash_runtime_to_storage_addr(XIP_BASE+FU_SLOT_SIZE-1)!=(intptr_t)(XIP_BASE+current+FU_SLOT_SIZE-1)) return false;
    e->boot=boot.partition;
    e->tbyb=!!(boot.tbyb_and_update_info&BOOT_TBYB_AND_UPDATE_FLAG_BUY_PENDING);
    e->valid=true; return true;
}
static bool maintenance(void) {
    qn_boot_hold_all_reset(); /* Physical RF off, no persistent host configuration changes. */
    if(!audio_engine_enter_update()) return false;
    for(unsigned i=0;i<NUM_DMA_CHANNELS;++i) if(dma_channel_is_busy(i)) return false;
    quiet=true; return true;
}
/* SDK 2.3.1 rom_flash_op/rom_explicit_buy provide CPU lockout only; the
 * underlying A2-A4 ROM routines do not exit a boot-selected continuous-read
 * mode or restore it. One SRAM callback spans exit-XIP, the ROM call, and XIP restoration.
 * Never return to flash or release core1/IRQs between those operations. Use
 * SDK-declared raw ROM entry points to avoid nesting flash_safe_execute.
 * flash_start_xip restores the boot XIP setup; neither it nor this path resets
 * ATRANS. This board has CS0 flash only and no custom QMI/pad configuration.
 * It is NOT a general save/restore for application-configured CS1 PSRAM. */
typedef struct {
    cflash_flags_t flags;
    uint32_t address, length;
    uint8_t *buffer;
    bool buy;
    int result;
} flash_call_t;
static void __no_inline_not_in_flash_func(flash_call)(void *param) {
    flash_call_t *call=param;
    rom_flash_op_fn op=(rom_flash_op_fn)rom_func_lookup_inline(ROM_FUNC_FLASH_OP);
    rom_explicit_buy_fn buy=(rom_explicit_buy_fn)rom_func_lookup_inline(ROM_FUNC_EXPLICIT_BUY);
    rom_connect_internal_flash();
    rom_flash_exit_xip();
    rom_flash_flush_cache();
    call->result=call->buy ? buy(call->buffer,call->length) :
        op(call->flags,call->address,call->length,call->buffer);
    flash_start_xip();
}
static bool operation(unsigned op,uint32_t off,uint8_t *buf,uint32_t n) {
    if(off>=0x200000u || n>0x200000u-off) return false;
    if(op!=CFLASH_OP_VALUE_READ) {
        fu_environment_t e;
        if(!quiet || !environment(&e)) return false;
        uint32_t inactive=e.boot ? FU_SLOT_A : FU_SLOT_B;
        bool slot=off>=inactive && off<inactive+FU_SLOT_SIZE && n<=inactive+FU_SLOT_SIZE-off;
        bool metadata=off>=FU_META_A && n<=0x200000u-off;
        if(!slot && !metadata) return false;
        if(op==CFLASH_OP_VALUE_ERASE && (off%4096 || n!=4096)) return false;
        if(op==CFLASH_OP_VALUE_PROGRAM && (off%256 || n!=256)) return false;
    }
    cflash_flags_t f={.flags=(op<<CFLASH_OP_LSB)|(CFLASH_SECLEVEL_VALUE_SECURE<<CFLASH_SECLEVEL_LSB)|
        (CFLASH_ASPACE_VALUE_STORAGE<<CFLASH_ASPACE_LSB)};
    if(op==CFLASH_OP_VALUE_READ) return rom_flash_op(f,XIP_BASE+off,n,buf)==0;
    if(!bootrom_try_acquire_lock(BOOTROM_LOCK_FLASH_OP)) return false;
    flash_call_t call={.flags=f,.address=XIP_BASE+off,.length=n,.buffer=buf,.result=PICO_ERROR_GENERIC};
    int rc=flash_safe_execute(flash_call,&call,1000);
    bootrom_release_lock(BOOTROM_LOCK_FLASH_OP);
    return rc==PICO_OK && call.result==0;
}
static bool read_flash(uint32_t off,uint8_t *p,uint32_t n) { return operation(CFLASH_OP_VALUE_READ,off,p,n); }
static bool erase_flash(uint32_t off) {
    if(!operation(CFLASH_OP_VALUE_ERASE,off,NULL,4096)) return false;
    uint8_t check[256];
    for(unsigned pos=0;pos<4096;pos+=256) {
        if(!read_flash(off+pos,check,256)) return false;
        for(unsigned i=0;i<256;++i) if(check[i]!=255) return false;
    }
    return true;
}
static bool program_flash(uint32_t off,const uint8_t *p) { return operation(CFLASH_OP_VALUE_PROGRAM,off,(uint8_t *)p,256); }
static uint32_t word(const uint8_t *p) { return host_get_u32(p); }
/* Only the native SDK ARM-S XIP, two-block, unsigned IMAGE_DEF produced by this
 * build is supported. Reject load maps, extra blocks, signatures and OTP rollback.
 * Those require a separate security design, not permissive best-effort parsing. */
static int image_header(uint32_t off,uint32_t n,bool tbyb,uint16_t major,uint16_t minor) {
    uint8_t head[512],tail[20];
    if(n<512 || !read_flash(off,head,sizeof(head))) return -1;
    if(word(head)<0x20000000u || word(head)>0x20082000u || word(head)%8 ||
       !(word(head+4)&1) || word(head+4)<XIP_BASE || word(head+4)>=XIP_BASE+n) return -1;
    for(unsigned i=8;i+28<=sizeof(head);i+=4) {
        if(word(head+i)!=PICOBIN_BLOCK_MARKER_START) continue;
        uint32_t type=(tbyb ? 0x9021u : 0x1021u)<<16|0x142u;
        if(word(head+i+4)!=type || word(head+i+8)!=0x248u ||
           word(head+i+12)!=((uint32_t)major<<16|minor) || word(head+i+16)!=0x3ffu ||
           word(head+i+24)!=PICOBIN_BLOCK_MARKER_END) return -1;
        uint32_t delta=word(head+i+20);
        if(delta>n || i>n-delta || n-(i+delta)!=20 || !read_flash(off+i+delta,tail,20)) return -1;
        if(word(tail)!=PICOBIN_BLOCK_MARKER_START || word(tail+4)!=0x1feu ||
           word(tail+8)!=0x1ffu || word(tail+12)!=0u-delta || word(tail+16)!=PICOBIN_BLOCK_MARKER_END) return -1;
        return (int)i+7; /* byte containing the native TBYB bit */
    }
    return -1;
}
static bool image(uint32_t off,uint32_t n,bool tbyb,uint16_t major,uint16_t minor,const uint8_t *id) {
    if(image_header(off,n,tbyb,major,minor)<0) return false;
    uint8_t data[256+31],needle[32];
    for(unsigned j=0;j<16;++j) needle[j]=identity.magic[j]; memcpy(needle+16,id,16);
    for(uint32_t pos=0;pos<n;pos+=256) {
        uint32_t len=n-pos<sizeof(data) ? n-pos : sizeof(data);
        if(!read_flash(off+pos,data,len)) return false;
        for(unsigned j=0;j+32<=len;++j) if(!memcmp(data+j,needle,32)) return true;
    }
    return false;
}
/* Identity is read from the verified binary, never supplied as an upload ID. */
static bool inspect(uint32_t off,uint32_t n,uint16_t *major,uint16_t *minor,uint8_t *id) {
    uint8_t data[256+51],magic[16]; bool found=false;
    for(unsigned j=0;j<16;++j) magic[j]=identity.magic[j];
    for(uint32_t pos=0;pos<n;pos+=256) {
        uint32_t len=n-pos<sizeof(data) ? n-pos : sizeof(data);
        if(!read_flash(off+pos,data,len)) return false;
        for(unsigned j=0;j<256 && j+52<=len;++j) if(!memcmp(data+j,magic,16)) {
            if(found) return false;
            found=true; memcpy(id,data+j+16,16);
            *major=data[j+48]|(uint16_t)data[j+49]<<8;
            *minor=data[j+50]|(uint16_t)data[j+51]<<8;
        }
    }
    return found && image_header(off,n,true,*major,*minor)>=0;
}
static bool hash(uint32_t off,uint32_t n,bool normalize,uint8_t *out) {
    int flag=-1;
    if(normalize) {
        flag=image_header(off,n,false,identity.major,identity.minor);
        if(flag<0) return false;
    }
    pico_sha256_state_t sha;
    if(pico_sha256_try_start(&sha,SHA256_BIG_ENDIAN,false)!=PICO_OK) return false;
    uint8_t block[256]; bool ok=true;
    /* rom_flash_op does not use SHA256; never nest partition-table queries here. */
    for(uint32_t pos=0;pos<n;pos+=sizeof(block)) {
        uint32_t len=n-pos<sizeof(block) ? n-pos : sizeof(block);
        if(!read_flash(off+pos,block,len)) { ok=false; break; }
        if(flag>=0 && (uint32_t)flag>=pos && (uint32_t)flag<pos+len) block[flag-pos]|=0x80;
        pico_sha256_update_blocking(&sha,block,len);
    }
    sha256_result_t result; pico_sha256_finish(&sha,&result);
    if(ok) memcpy(out,&result,32);
    return ok;
}
static bool buy(void) {
    if(!quiet) return false;
    flash_call_t call={.buy=true,.buffer=(uint8_t *)scratch,.length=sizeof(scratch),.result=PICO_ERROR_GENERIC};
    int rc=flash_safe_execute(flash_call,&call,1000);
    if(rc==PICO_OK) rc=call.result;
    if(rc!=0) {
        /* ROM disables the trial watchdog at entry, even on failure. Return to
         * normal boot selection (old non-TBYB image), never leave a failed buy hung. */
        rom_reboot(REBOOT2_FLAG_REBOOT_TYPE_NORMAL|REBOOT2_FLAG_NO_RETURN_ON_SUCCESS,10,0,0);
        return false;
    }
    return true;
}
static bool reboot(uint32_t off) {
    fu_environment_t e;
    if(off==FU_NORMAL_REBOOT) {
        if(!environment(&e) || e.tbyb!=0) return false;
        return rom_reboot(REBOOT2_FLAG_REBOOT_TYPE_NORMAL|REBOOT2_FLAG_NO_RETURN_ON_SUCCESS,10,0,0)==0;
    }
    if(!environment(&e) || off!=(e.boot ? FU_SLOT_A : FU_SLOT_B)) return false;
    return rom_reboot(REBOOT2_FLAG_REBOOT_TYPE_FLASH_UPDATE|REBOOT2_FLAG_NO_RETURN_ON_SUCCESS,
                      10,XIP_BASE+off,0)==0;
}
void flash_update_platform_init(bool ready) {
    initialized=ready;
    static const fu_ops_t ops={environment,maintenance,read_flash,erase_flash,program_flash,hash,image,inspect,buy,reboot};
    flash_update_init(&ops);
}
