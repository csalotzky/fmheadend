#include "flash_update.h"
#include <string.h>
/* Two committed records, each in its own erase sector. Data page first, seal
 * page last; never erase the currently selected record. Torn records fail CRC.
 * Upload progress is RAM-only: after a reset an unverified session MUST restart. */
static const fu_ops_t *io;
static fu_environment_t env;
static uint8_t state, error, target=255, session[16], digest[32], build[16];
static uint16_t major, minor;
static uint32_t size, accepted, committed, sequence, record_sector;
static bool maintenance, maintenance_requested, begin_unpersisted;
static uint8_t reboot_command;
static _Alignas(4) uint8_t page[256];
static uint32_t get32(const uint8_t *p) { return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24; }
static void put32(uint8_t *p,uint32_t v) { for(unsigned i=0;i<4;++i) p[i]=v>>(8*i); }
static uint32_t crc32(const uint8_t *p,size_t n) {
    uint32_t c=~0u;
    while(n--) { c^=*p++; for(unsigned i=0;i<8;++i) c=(c>>1)^((0u-(c&1u))&0xedb88320u); }
    return ~c;
}
static uint32_t base(void) { return target==0 ? FU_SLOT_A : FU_SLOT_B; }
static bool layout(void) {
    return io->environment(&env) && env.valid && env.boot<2;
}
static bool quiesce(void) {
    maintenance_requested=true;
    if (!maintenance) maintenance=io->maintenance();
    return maintenance;
}
static bool record_read(uint32_t sector,uint8_t *data) {
    uint8_t seal[256];
    return io->read(sector,data,256) && io->read(sector+256,seal,256) &&
        get32(data)==0x31554648u && get32(data+252)==crc32(data,252) &&
        get32(seal)==0x4c414553u && get32(seal+4)==get32(data+4) &&
        get32(seal+8)==get32(data+252) && get32(seal+252)==crc32(seal,252);
}
static bool persist(uint8_t next) {
    if(sequence==UINT32_MAX) return false;
    uint8_t data[256],seal[256],check[256];
    memset(data,0xff,sizeof(data)); memset(seal,0xff,sizeof(seal));
    put32(data,0x31554648u); put32(data+4,sequence+1);
    data[8]=next; data[9]=target; data[10]=FU_FORMAT;
    put32(data+12,size); memcpy(data+16,session,16); memcpy(data+32,digest,32);
    memcpy(data+64,build,16); put32(data+80,(uint32_t)major<<16|minor);
    put32(data+252,crc32(data,252));
    put32(seal,0x4c414553u); put32(seal+4,sequence+1); put32(seal+8,get32(data+252));
    put32(seal+252,crc32(seal,252));
    uint32_t dest=record_sector==FU_META_A ? FU_META_B : FU_META_A;
    if(!io->erase(dest) || !io->program(dest,data) || !io->read(dest,check,256) ||
       memcmp(data,check,256) || !io->program(dest+256,seal) ||
       !record_read(dest,check) || memcmp(data,check,256)) return false;
    record_sector=dest; ++sequence; state=next; return true;
}
static bool verified(bool bought) {
    uint8_t actual[32];
    return target<2 && size>=512 && size<=FU_SLOT_SIZE &&
        io->hash(base(),size,bought,actual) && !memcmp(actual,digest,32) &&
        io->image(base(),size,!bought,major,minor,build);
}
void flash_update_init(const fu_ops_t *ops) {
    io=ops; state=FU_IDLE; error=0; target=255; size=accepted=committed=sequence=record_sector=0;
    maintenance=maintenance_requested=begin_unpersisted=false; reboot_command=0; memset(session,0,16); memset(digest,0,32); memset(build,0,16);
    if(!layout()) { error=FU_E_LAYOUT; return; }
    uint8_t a[256],b[256],*r=NULL;
    bool va=record_read(FU_META_A,a),vb=record_read(FU_META_B,b);
    if(va) { r=a; record_sector=FU_META_A; }
    if(vb && (!va || get32(b+4)>get32(a+4))) { r=b; record_sector=FU_META_B; }
    if(!r) return;
    sequence=get32(r+4); target=r[9]; state=r[8]; size=get32(r+12);
    if(r[10]!=FU_FORMAT || target>1 || size<512 || size>FU_SLOT_SIZE || state>FU_ABORTED) {
        state=FU_FAILED; error=FU_E_IMAGE; target=255; return;
    }
    memcpy(session,r+16,16); memcpy(digest,r+32,32); memcpy(build,r+64,16);
    major=get32(r+80)>>16; minor=get32(r+80);
    if(state==FU_RECEIVING) state=FU_INTERRUPTED;
    if(state==FU_VERIFIED || state==FU_ACTIVATING) {
        accepted=committed=size;
        if(target==env.boot && state==FU_ACTIVATING && !memcmp(env.build,build,16)) {
            if(!quiesce()) { state=FU_FAILED; error=FU_E_QUIESCE; return; }
            if(env.tbyb==0 && verified(true)) {
                state=FU_CONFIRMED;
                if(!persist(FU_CONFIRMED)) error=FU_E_FLASH;
            }
            else if(env.tbyb!=1 || !verified(false)) { state=FU_FAILED; error=FU_E_IMAGE; }
        }
    }
}
static void status(uint8_t *r) {
    layout(); memset(r,0,FU_REPLY_SIZE);
    r[0]=FU_FORMAT; r[1]=state; r[2]=env.valid ? env.boot : 255; r[3]=target;
    r[4]=env.valid ? env.tbyb : 255; r[5]=error; r[6]=maintenance_requested; r[7]=env.ready;
    put32(r+8,accepted); put32(r+12,committed); put32(r+16,size);
    memcpy(r+20,session,16); memcpy(r+36,digest,32); memcpy(r+68,build,16);
    memcpy(r+84,env.build,16); put32(r+100,(uint32_t)env.minor<<16|env.major);
    memcpy(r+104,env.version,16); put32(r+120,FU_SLOT_SIZE); r[124]=FU_CHUNK;
    r[126]=env.valid ? (env.boot^1u) : 255;
}
static uint8_t fail(uint8_t e) { error=e; return e==FU_E_FLASH || e==FU_E_ROM || e==FU_E_QUIESCE ? 6 : 3; }
bool flash_update_maintenance(void) { return maintenance_requested; }
void flash_update_ack_consumed(uint8_t command) {
    if(!reboot_command || command!=reboot_command) return;
    reboot_command=0;
    if(!io->reboot(command==FU_ACTIVATE ? base() : FU_NORMAL_REBOOT)) error=FU_E_ROM;
}
uint8_t flash_update_execute(const uint8_t *p,size_t n,uint8_t *r) {
    if(!n) return fail(FU_E_ARGUMENT);
    uint8_t cmd=p[0];
    if(cmd<FU_BEGIN || cmd>FU_ABORT) return 1;
    if(cmd==FU_STATUS) { if(n!=1) return fail(FU_E_ARGUMENT); status(r); return 0; }
    if(!layout()) return fail(FU_E_LAYOUT);
    if(cmd==FU_BEGIN) {
        if(n!=FU_BEGIN_SIZE || p[73]!=FU_FORMAT || get32(p+17)<512 || get32(p+17)>FU_SLOT_SIZE)
            return fail(FU_E_ARGUMENT);
        uint32_t version=(uint32_t)p[69]|(uint32_t)p[70]<<8|(uint32_t)p[71]<<16|(uint32_t)p[72]<<24;
        uint16_t new_major=version&65535u,new_minor=version>>16;
        /* ROM flash-update selection permits equal/older versions. At buy it
         * erases the other IMAGE_DEF if needed for subsequent normal boot. */
        if(env.tbyb!=0 || !env.ready)
            return fail(FU_E_STATE);
        if(!memcmp(session,p+1,16) && state!=FU_IDLE && state!=FU_ABORTED) {
            if(size!=get32(p+17) || memcmp(digest,p+21,32) || memcmp(build,p+53,16) || major!=new_major || minor!=new_minor)
                return fail(FU_E_SESSION);
            if(state==FU_RECEIVING || state==FU_VERIFIED) { status(r); return 0; }
            if(state!=FU_INTERRUPTED) return fail(FU_E_STATE);
        } else if(state==FU_RECEIVING || state==FU_VERIFIED || state==FU_ACTIVATING) return fail(FU_E_SESSION);
        target=env.boot^1u; size=get32(p+17); major=new_major; minor=new_minor;
        memcpy(session,p+1,16); memcpy(digest,p+21,32); memcpy(build,p+53,16);
        accepted=committed=0; reboot_command=0; memset(page,0xff,256);
        /* Retain the attempted session for recovery even if stopping DMA fails.
         * No flash has been touched yet; ABORT can request a safe normal reset. */
        begin_unpersisted=true;
        if(!quiesce()) { state=FU_FAILED; return fail(FU_E_QUIESCE); }
        if(!persist(FU_RECEIVING)) { state=FU_FAILED; return fail(FU_E_FLASH); }
        begin_unpersisted=false;
        if(!io->erase(base())) { state=FU_FAILED; return fail(FU_E_FLASH); }
    } else {
        if(n<17 || memcmp(session,p+1,16)) return fail(FU_E_SESSION);
        if(cmd!=FU_WRITE && n!=17) return fail(FU_E_ARGUMENT);
        if(cmd==FU_ABORT) {
            if(env.tbyb!=0 || target>1 || target==env.boot) return fail(FU_E_STATE);
            if(state==FU_ABORTED && !maintenance_requested) { error=0; status(r); return 0; }
            if(begin_unpersisted && !maintenance) {
                /* Partial stop: no flash writes are safe. The old bought image
                 * can boot normally; this RAM-only cancellation needs no journal. */
                state=FU_ABORTED;
            } else {
                if(!quiesce()) return fail(FU_E_QUIESCE);
                if(!persist(FU_ABORTED)) return fail(FU_E_FLASH);
            }
            reboot_command=FU_ABORT;
        } else if(cmd==FU_CONFIRM) {
            if((state!=FU_ACTIVATING && state!=FU_CONFIRMED) || target!=env.boot ||
               env.tbyb>1 || !env.ready || memcmp(env.build,build,16)) return fail(FU_E_STATE);
            if(state==FU_CONFIRMED && env.tbyb==0 && !maintenance_requested) { error=0; status(r); return 0; }
            if(!quiesce()) return fail(FU_E_QUIESCE);
            if(env.tbyb==1) {
                if(state!=FU_ACTIVATING || !verified(false)) return fail(FU_E_IMAGE);
                /* ROM mutates recent_boot even on some failing buys. Neither
                 * pending=0 nor flash content alone substitutes for this result. */
                if(!io->buy() || !layout() || env.tbyb!=0) {
                    /* A failed buy can already have cleared BUY_PENDING. Do
                     * not let a retry reinterpret that as successful buy. */
                    state=FU_FAILED; return fail(FU_E_ROM);
                }
            }
            if(!verified(true)) return fail(FU_E_ROM);
            /* Also repairs a lost CONFIRM ACK / torn journal after a successful
             * buy. Never buy twice and never reboot before this durable record. */
            if(!persist(FU_CONFIRMED)) return fail(FU_E_FLASH);
            reboot_command=FU_CONFIRM;
        } else {
            if(target==env.boot || env.tbyb!=0) return fail(FU_E_STATE);
            if(!quiesce()) return fail(FU_E_QUIESCE);
            if(cmd==FU_WRITE) {
                if(n<23 || p[21]==0 || p[21]>FU_CHUNK || n!=22u+p[21] || state!=FU_RECEIVING) return fail(FU_E_ARGUMENT);
                uint32_t off=get32(p+17),len=p[21];
                if(off>size || len>size-off || off>accepted) return fail(FU_E_OFFSET);
                if(off<accepted) {
                    if(len>accepted-off) return fail(FU_E_OFFSET);
                    uint8_t old[FU_CHUNK];
                    for(uint32_t i=0;i<len;++i) {
                        if(off+i<committed) { if(!io->read(base()+off+i,old+i,1)) return fail(FU_E_FLASH); }
                        else old[i]=page[(off+i)%256];
                    }
                    if(memcmp(old,p+22,len)) return fail(FU_E_OFFSET);
                    status(r); return 0;
                }
                /* Fixed 128-byte chunks except the final tail: never cross a page. */
                if(off%FU_CHUNK || (len!=FU_CHUNK && off+len!=size)) return fail(FU_E_OFFSET);
                if(off && off%4096==0 && !io->erase(base()+off)) { state=FU_FAILED; return fail(FU_E_FLASH); }
                memcpy(page+off%256,p+22,len); accepted+=len;
                if(accepted%256==0 || accepted==size) {
                    uint8_t check[256]; uint32_t page_off=off&~255u;
                    if(!io->program(base()+page_off,page) || !io->read(base()+page_off,check,256) || memcmp(page,check,256)) {
                        state=FU_FAILED; return fail(FU_E_FLASH);
                    }
                    committed=accepted; memset(page,0xff,256);
                }
            } else if(cmd==FU_VERIFY) {
                if(state!=FU_RECEIVING && state!=FU_VERIFIED) return fail(FU_E_STATE);
                if(accepted!=size || committed!=size) return fail(FU_E_OFFSET);
                if(!verified(false)) { state=FU_FAILED; return fail(FU_E_HASH); }
                if(!persist(FU_VERIFIED)) { state=FU_FAILED; return fail(FU_E_FLASH); }
            } else if(cmd==FU_ACTIVATE) {
                if((state!=FU_VERIFIED && state!=FU_ACTIVATING) || !verified(false)) return fail(FU_E_STATE);
                if(!persist(FU_ACTIVATING)) { state=FU_FAILED; return fail(FU_E_FLASH); }
                reboot_command=FU_ACTIVATE;
            }
        }
    }
    error=0; status(r); return 0;
}
