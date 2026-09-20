#include "qn_boot.h"

#include "pico/stdlib.h"
#include "board.h"

#define QN_SYSTEM1 0x00u
#define QN_DEV_ADD 0x02u
#define QN_CID2 0x06u
#define QN_SYSTEM1_TX_OFF 0x05u /* Reference value, with TXREQ clear. */
#define QN_SYSTEM1_TXREQ 0x40u

void qn_boot_hold_all_reset(void)
{
    for (unsigned index = 0; index < QN_COUNT; ++index) {
        const unsigned pin = QN_CEN_FIRST_PIN + index;
        gpio_init(pin);
        gpio_put(pin, 0); /* Set latch before output: no high glitch. */
        gpio_set_dir(pin, GPIO_OUT);
    }
}

qn_boot_result_t qn_boot_device(unsigned index)
{
    qn_boot_result_t result = {
        .address = QN_FIRST_ADDR + index,
        .stage = "invalid index",
        .bus_status = QN_BUS_INVALID_ARGUMENT,
    };
    if (index >= QN_COUNT) return result;

    const unsigned cen_pin = QN_CEN_FIRST_PIN + index;
    uint8_t initial_cid2, system1;
    gpio_put(cen_pin, 1);
    sleep_ms(100);

    result.stage = "default CID2 read";
    result.bus_status = qn_bus_read_reg(QN_DEFAULT_ADDR, QN_CID2, &result.cid2);
    if (result.bus_status != QN_BUS_OK) goto failed;
    result.cid2_read = true;
    result.stage = "default CID2 identity";
    if ((result.cid2 & 0xf0u) != 0x10u) goto failed;
    initial_cid2 = result.cid2;

    /* CEN reset starts RF off. Explicitly hold it off before changing address.
     * No SWRST: it would restore DEV_ADD to the shared default address.
     */
    result.stage = "default RF OFF write";
    result.bus_status = qn_bus_write_reg(QN_DEFAULT_ADDR, QN_SYSTEM1, QN_SYSTEM1_TX_OFF);
    if (result.bus_status != QN_BUS_OK) goto failed;

    result.stage = "address assignment";
    result.bus_status = qn_bus_write_reg(QN_DEFAULT_ADDR, QN_DEV_ADD, result.address);
    if (result.bus_status != QN_BUS_OK) goto failed;
    sleep_ms(10);

    result.stage = "assigned CID2 read";
    result.cid2_read = false;
    result.bus_status = qn_bus_read_reg(result.address, QN_CID2, &result.cid2);
    if (result.bus_status != QN_BUS_OK) goto failed;
    result.cid2_read = true;
    result.stage = "assigned CID2 identity";
    if ((result.cid2 & 0xf0u) != 0x10u || result.cid2 != initial_cid2) goto failed;

    result.stage = "assigned RF OFF write";
    result.bus_status = qn_bus_write_reg(result.address, QN_SYSTEM1, QN_SYSTEM1_TX_OFF);
    if (result.bus_status != QN_BUS_OK) goto failed;
    result.stage = "RF OFF readback";
    result.bus_status = qn_bus_read_reg(result.address, QN_SYSTEM1, &system1);
    if (result.bus_status != QN_BUS_OK) goto failed;
    result.stage = "RF OFF verification";
    if (system1 & QN_SYSTEM1_TXREQ) goto failed;

    result.stage = "ready";
    result.ready = true;
    return result; /* Keep successfully assigned devices enabled. */

failed:
    /* Handles ambiguous DEV_ADD writes without leaving a 0x2A collision. */
    gpio_put(cen_pin, 0);
    return result;
}
