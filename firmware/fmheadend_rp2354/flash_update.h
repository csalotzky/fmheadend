#ifndef FMHEADEND_FLASH_UPDATE_H
#define FMHEADEND_FLASH_UPDATE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define FU_SLOT_SIZE 0xf0000u
#define FU_SLOT_A 0x4000u
#define FU_SLOT_B 0xf4000u
#define FU_META_A 0x1fe000u
#define FU_META_B 0x1ff000u
#define FU_REPLY_SIZE 112u
#define FU_WRITE_REPLY_SIZE 9u
#define FU_CHUNK 250u
#define FU_BEGIN_SIZE 37u
#define FU_FORMAT 2u
/* Internal reboot callback sentinel, never a wire offset. */
#define FU_NORMAL_REBOOT UINT32_MAX
#define FU_COMMAND 0xf0u
enum { FU_BEGIN=1, FU_WRITE, FU_STATUS, FU_VERIFY, FU_ACTIVATE, FU_CONFIRM };
enum { FU_IDLE, FU_RECEIVING, FU_VERIFIED, FU_ACTIVATING, FU_CONFIRMED,
       FU_INTERRUPTED, FU_FAILED };
enum { FU_E_NONE, FU_E_LAYOUT, FU_E_ARGUMENT, FU_E_OFFSET=4,
       FU_E_FLASH, FU_E_HASH, FU_E_IMAGE, FU_E_STATE, FU_E_QUIESCE, FU_E_ROM };
typedef struct {
    uint8_t boot, tbyb;
    uint16_t major, minor;
    uint8_t build[16];
    char version[16];
    bool valid, ready;
} fu_environment_t;
/* Storage offsets, never translated runtime pointers. All callbacks foreground. */
typedef struct {
    bool (*environment)(fu_environment_t *);
    bool (*maintenance)(void);
    bool (*read)(uint32_t, uint8_t *, uint32_t);
    bool (*erase)(uint32_t);
    bool (*program)(uint32_t, const uint8_t *);
    bool (*hash)(uint32_t, uint32_t, bool, uint8_t *);
    bool (*image)(uint32_t, uint32_t, bool, uint16_t, uint16_t, const uint8_t *);
    bool (*inspect)(uint32_t, uint32_t, uint16_t *, uint16_t *, uint8_t *);
    bool (*buy)(void);
    bool (*reboot)(uint32_t);
} fu_ops_t;
void flash_update_init(const fu_ops_t *ops);
uint8_t flash_update_execute(const uint8_t *payload, size_t length, uint8_t *reply);
bool flash_update_maintenance(void);
void flash_update_ack_consumed(uint8_t command);
/* Hardware adapter; no dependency on it in the state-machine host tests. */
void flash_update_platform_init(bool ready);
#endif
