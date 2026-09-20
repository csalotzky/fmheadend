#ifndef TEST_HARDWARE_I2C_H
#define TEST_HARDWARE_I2C_H
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    uint32_t intr_stat, tx_abrt_source, clr_tx_abrt, clr_rx_over, data_cmd;
    uint32_t clr_restart_det, clr_stop_det, clr_rx_done, clr_rd_req;
    uint32_t enable, con, ack_general_call, dma_cr, clr_intr, intr_mask;
} i2c_hw_t;
typedef struct { i2c_hw_t hw; } i2c_inst_t;
extern i2c_inst_t test_i2c1;
#define i2c1 (&test_i2c1)
#define I2C_IC_INTR_STAT_R_RX_FULL_BITS (1u << 2)
#define I2C_IC_INTR_STAT_R_RX_OVER_BITS (1u << 3)
#define I2C_IC_INTR_STAT_R_RD_REQ_BITS (1u << 5)
#define I2C_IC_INTR_STAT_R_TX_ABRT_BITS (1u << 6)
#define I2C_IC_INTR_STAT_R_RX_DONE_BITS (1u << 7)
#define I2C_IC_INTR_STAT_R_STOP_DET_BITS (1u << 9)
#define I2C_IC_INTR_STAT_R_RESTART_DET_BITS (1u << 12)
#define I2C_IC_INTR_MASK_M_RX_FULL_BITS I2C_IC_INTR_STAT_R_RX_FULL_BITS
#define I2C_IC_INTR_MASK_M_RX_OVER_BITS I2C_IC_INTR_STAT_R_RX_OVER_BITS
#define I2C_IC_INTR_MASK_M_RD_REQ_BITS I2C_IC_INTR_STAT_R_RD_REQ_BITS
#define I2C_IC_INTR_MASK_M_TX_ABRT_BITS I2C_IC_INTR_STAT_R_TX_ABRT_BITS
#define I2C_IC_INTR_MASK_M_RX_DONE_BITS I2C_IC_INTR_STAT_R_RX_DONE_BITS
#define I2C_IC_INTR_MASK_M_STOP_DET_BITS I2C_IC_INTR_STAT_R_STOP_DET_BITS
#define I2C_IC_INTR_MASK_M_RESTART_DET_BITS I2C_IC_INTR_STAT_R_RESTART_DET_BITS
#define I2C_IC_TX_ABRT_SOURCE_TX_FLUSH_CNT_BITS 0xff800000u
#define I2C_IC_DATA_CMD_FIRST_DATA_BYTE_BITS (1u << 11)
#define I2C_IC_CON_STOP_DET_IFADDRESSED_BITS (1u << 7)
i2c_hw_t *i2c_get_hw(i2c_inst_t *i2c);
unsigned i2c_get_read_available(i2c_inst_t *i2c);
void i2c_init(i2c_inst_t *i2c, unsigned baud);
void i2c_set_slave_mode(i2c_inst_t *i2c, bool slave, uint8_t address);
#endif
