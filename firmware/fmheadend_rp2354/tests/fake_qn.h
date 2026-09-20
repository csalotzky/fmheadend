#ifndef TEST_FAKE_QN_H
#define TEST_FAKE_QN_H
#include <stdbool.h>
#include <stdint.h>
extern uint8_t fake_regs[8][256];
extern bool fake_cen[8], fake_retry_twice;
extern unsigned fake_calls, fake_fail_at, fake_fail_count, fake_writes;
extern uint64_t fake_now;
extern int fake_mismatch_reg;
extern void (*fake_operation_hook)(void);
void fake_qn_reset(void);
#endif
