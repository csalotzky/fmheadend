#include "qn_bus.h"

#include "pico/stdlib.h"
#include "board.h"

#define SCL_TIMEOUT_US 1000u
#define TRANSACTION_TIMEOUT_US 10000u

/* Adapted from fmheadend_i2ctest: preserve its 5 us delays and open-drain
 * transitions on the normal path. Never drive either line HIGH.
 */
static void bus_delay(void) { sleep_us(5); }
static void sda_release(void) { gpio_set_dir(QN_SDA_PIN, GPIO_IN); }
static void scl_release(void) { gpio_set_dir(QN_SCL_PIN, GPIO_IN); }

static void sda_low(void)
{
    gpio_put(QN_SDA_PIN, 0);
    gpio_set_dir(QN_SDA_PIN, GPIO_OUT);
}

static void scl_low(void)
{
    gpio_put(QN_SCL_PIN, 0);
    gpio_set_dir(QN_SCL_PIN, GPIO_OUT);
}

static qn_bus_status_t scl_high(uint64_t deadline)
{
    scl_release();
    bus_delay();
    const uint64_t clock_deadline = time_us_64() + SCL_TIMEOUT_US;
    while (!gpio_get(QN_SCL_PIN)) {
        const uint64_t now = time_us_64();
        if (now >= deadline) return QN_BUS_TRANSACTION_TIMEOUT;
        if (now >= clock_deadline) return QN_BUS_SCL_TIMEOUT;
        sleep_us(1);
    }
    return time_us_64() >= deadline ? QN_BUS_TRANSACTION_TIMEOUT : QN_BUS_OK;
}

static qn_bus_status_t tw_start(uint64_t deadline)
{
    sda_release();
    qn_bus_status_t status = scl_high(deadline);
    if (status != QN_BUS_OK) return status;
    if (!gpio_get(QN_SDA_PIN)) return QN_BUS_SDA_STUCK;
    sda_low();
    bus_delay();
    scl_low();
    bus_delay();
    return QN_BUS_OK;
}

static qn_bus_status_t tw_stop(uint64_t deadline)
{
    sda_low();
    bus_delay();
    qn_bus_status_t status = scl_high(deadline);
    sda_release();
    bus_delay();
    if (status == QN_BUS_OK && !gpio_get(QN_SDA_PIN)) {
        status = QN_BUS_SDA_STUCK;
    }
    return status;
}

static qn_bus_status_t tw_write_byte(uint8_t value, uint64_t deadline)
{
    for (int bit = 7; bit >= 0; --bit) {
        if (value & (1u << bit)) sda_release();
        else sda_low();
        bus_delay();
        qn_bus_status_t status = scl_high(deadline);
        if (status != QN_BUS_OK) return status;
        /* Do not mistake a stuck-low bus for a series of ACKs. */
        if ((value & (1u << bit)) && !gpio_get(QN_SDA_PIN)) {
            return QN_BUS_SDA_STUCK;
        }
        scl_low();
        bus_delay();
    }
    sda_release();
    bus_delay();
    qn_bus_status_t status = scl_high(deadline);
    if (status != QN_BUS_OK) return status;
    const bool ack = gpio_get(QN_SDA_PIN) == 0;
    scl_low();
    bus_delay();
    return ack ? QN_BUS_OK : QN_BUS_NACK;
}

static qn_bus_status_t tw_read_byte_nack(uint8_t *value, uint64_t deadline)
{
    uint8_t received = 0;
    sda_release();
    for (int bit = 7; bit >= 0; --bit) {
        qn_bus_status_t status = scl_high(deadline);
        if (status != QN_BUS_OK) return status;
        if (gpio_get(QN_SDA_PIN)) received |= 1u << bit;
        scl_low();
        bus_delay();
    }
    /* NACK the final (only) byte, leaving SDA released for the ninth clock. */
    sda_release();
    bus_delay();
    qn_bus_status_t status = scl_high(deadline);
    if (status != QN_BUS_OK) return status;
    if (!gpio_get(QN_SDA_PIN)) return QN_BUS_SDA_STUCK;
    scl_low();
    bus_delay();
    *value = received;
    return QN_BUS_OK;
}

static qn_bus_status_t finish(qn_bus_status_t status, uint64_t deadline)
{
    /* Change SDA with SCL low even if an error interrupted a bit. */
    scl_low();
    const qn_bus_status_t stop_status = tw_stop(deadline);
    sda_release();
    scl_release();
    return status == QN_BUS_OK ? stop_status : status;
}

void qn_bus_init(void)
{
    gpio_init(QN_SDA_PIN);
    gpio_init(QN_SCL_PIN);
    gpio_pull_up(QN_SDA_PIN);
    gpio_pull_up(QN_SCL_PIN);
    sda_release();
    scl_release();
}

qn_bus_status_t qn_bus_write_reg(uint8_t address, uint8_t reg, uint8_t value)
{
    if (address < 0x08 || address > 0x77) return QN_BUS_INVALID_ARGUMENT;
    const uint64_t deadline = time_us_64() + TRANSACTION_TIMEOUT_US;
    qn_bus_status_t status = tw_start(deadline);
    if (status == QN_BUS_OK) status = tw_write_byte(address << 1, deadline);
    if (status == QN_BUS_OK) status = tw_write_byte(reg, deadline);
    if (status == QN_BUS_OK) status = tw_write_byte(value, deadline);
    return finish(status, deadline);
}

qn_bus_status_t qn_bus_read_reg(uint8_t address, uint8_t reg, uint8_t *value)
{
    if (address < 0x08 || address > 0x77 || value == NULL) {
        return QN_BUS_INVALID_ARGUMENT;
    }
    uint8_t received = 0;
    const uint64_t deadline = time_us_64() + TRANSACTION_TIMEOUT_US;
    qn_bus_status_t status = tw_start(deadline);
    /* QN grammar: address+READ, MASTER sends register, then receives data.
     * No address+WRITE prefix, repeated START or hardware-I2C read.
     */
    if (status == QN_BUS_OK) status = tw_write_byte((address << 1) | 1u, deadline);
    if (status == QN_BUS_OK) status = tw_write_byte(reg, deadline);
    if (status == QN_BUS_OK) status = tw_read_byte_nack(&received, deadline);
    status = finish(status, deadline);
    if (status == QN_BUS_OK) *value = received;
    return status;
}

const char *qn_bus_status_name(qn_bus_status_t status)
{
    switch (status) {
    case QN_BUS_OK: return "OK";
    case QN_BUS_NACK: return "NACK";
    case QN_BUS_SCL_TIMEOUT: return "SCL timeout";
    case QN_BUS_SDA_STUCK: return "SDA stuck low";
    case QN_BUS_TRANSACTION_TIMEOUT: return "transaction timeout";
    case QN_BUS_INVALID_ARGUMENT: return "invalid argument";
    default: return "unknown bus error";
    }
}
