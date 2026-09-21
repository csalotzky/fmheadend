#include "fake_qn.h"
#include "qn_bus.h"
#include <assert.h>
#include <string.h>
uint8_t fake_regs[8][256];
bool fake_cen[8], fake_retry_twice;
unsigned fake_calls, fake_fail_at, fake_fail_count, fake_writes;
uint64_t fake_now;
int fake_mismatch_reg;
void (*fake_operation_hook)(void);
void (*fake_write_hook)(unsigned target, uint8_t reg, uint8_t value);
void fake_qn_reset(void)
{
    memset(fake_regs,0,sizeof(fake_regs));
    for (unsigned i=0;i<8;++i) {
        fake_cen[i]=true;
        fake_regs[i][0]=5; fake_regs[i][1]=0x0b; fake_regs[i][2]=0x21+i;
        fake_regs[i][6]=0x11; fake_regs[i][0x5a]=0xaf;
    }
    fake_calls=fake_writes=fake_fail_at=fake_fail_count=0; fake_now=0;
    fake_retry_twice=false; fake_mismatch_reg=-1; fake_operation_hook=0;
    fake_write_hook=0;
}
static bool transaction(uint8_t address, unsigned duration)
{
    assert(address>=0x21 && address<=0x28);
    ++fake_calls;
    if (fake_operation_hook) fake_operation_hook();
    if (!fake_cen[address-0x21] || (fake_fail_at && fake_calls>=fake_fail_at &&
        fake_calls<fake_fail_at+fake_fail_count) || (fake_retry_twice && fake_calls%3)) {
        fake_now+=10010; return false;
    }
    fake_now+=fake_retry_twice ? 9990 : duration; return true;
}
qn_bus_status_t qn_bus_read_reg(uint8_t address,uint8_t reg,uint8_t *v)
{
    if (!transaction(address,395)) return QN_BUS_NACK;
    *v=fake_regs[address-0x21][reg];
    if (fake_mismatch_reg==reg) *v^=0x40;
    return QN_BUS_OK;
}
qn_bus_status_t qn_bus_write_reg(uint8_t address,uint8_t reg,uint8_t v)
{
    ++fake_writes;
    if (!transaction(address,435)) return QN_BUS_NACK;
    fake_regs[address-0x21][reg]=v;
    if (fake_write_hook) fake_write_hook(address-0x21,reg,v);
    return QN_BUS_OK;
}
void gpio_put(unsigned pin,bool v)
{
    assert(pin>=18 && pin<=25);
    fake_cen[pin-18]=v;
    if (!v) fake_regs[pin-18][0]=0;
}
void sleep_ms(uint32_t ms) { fake_now+=(uint64_t)ms*1000; }
#ifndef TEST_ADAPTER_CLOCK
uint64_t time_us_64(void) { return fake_now; }
#endif
