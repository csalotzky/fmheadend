/* Exercise the real driver against an open-drain GPIO/clock model. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "board.h"
#include "qn_bus.h"

static bool output[32], latch[32], active, reading;
static bool wire_released[64];
static unsigned pulse, starts, stops, nack_at, stuck_clock_at;
static bool stuck_sda, stuck_scl, stuck_stop;
static uint64_t now_us, released_at, stretch_us;
static uint8_t reply;

void gpio_init(unsigned pin) { output[pin] = latch[pin] = false; }
void gpio_put(unsigned pin, bool value)
{
    assert(pin == QN_SDA_PIN || pin == QN_SCL_PIN);
    assert(!value); /* The bus must never drive high. */
    latch[pin] = value;
}
void gpio_pull_up(unsigned pin) { assert(pin == 16 || pin == 17); }
void gpio_set_dir(unsigned pin, bool value)
{
    const bool was_output = output[pin];
    if (pin == QN_SDA_PIN && !output[QN_SCL_PIN]) {
        if (!was_output && value) {
            active = true;
            ++starts;
        } else if (was_output && !value && active) {
            active = false;
            ++stops;
        }
    }
    output[pin] = value;
    if (pin == QN_SCL_PIN && was_output && !value) {
        released_at = now_us;
        if (active) {
            ++pulse;
            assert(pulse < 64);
            wire_released[pulse] = !output[QN_SDA_PIN];
        }
    }
}
bool gpio_get(unsigned pin)
{
    if (output[pin]) return latch[pin];
    if (pin == QN_SCL_PIN) {
        return !stuck_scl && (!stuck_clock_at || pulse < stuck_clock_at) &&
               now_us - released_at >= stretch_us;
    }
    assert(pin == QN_SDA_PIN);
    if (stuck_sda || (stuck_stop && pulse >= 28)) return false;
    if (!active) return true;
    if (pulse == 9 || pulse == 18 || (!reading && pulse == 27)) {
        return pulse == nack_at;
    }
    if (reading && pulse >= 19 && pulse <= 26) {
        return (reply & (1u << (26 - pulse))) != 0;
    }
    return true;
}
void sleep_us(uint64_t us) { now_us += us; assert(now_us < 100000); }
uint64_t time_us_64(void) { return now_us; }

static void reset(bool read)
{
    memset(output, 0, sizeof(output));
    memset(latch, 0, sizeof(latch));
    memset(wire_released, 0, sizeof(wire_released));
    active = stuck_sda = stuck_scl = stuck_stop = false;
    pulse = starts = stops = nack_at = stuck_clock_at = 0;
    now_us = released_at = stretch_us = 0;
    reading = read;
    reply = 0x11;
    qn_bus_init();
}

static uint8_t wire_byte(unsigned first)
{
    uint8_t value = 0;
    for (unsigned bit = first; bit < first + 8; ++bit) {
        value = (value << 1) | wire_released[bit];
    }
    return value;
}

static void released(void)
{
    assert(!output[QN_SDA_PIN] && !output[QN_SCL_PIN]);
    assert(now_us <= 11000); /* Deadline plus bounded cleanup. */
}

int main(void)
{
    uint8_t value = 0xa5;
    reset(true);
    assert(qn_bus_read_reg(0x2a, 0x06, &value) == QN_BUS_OK);
    assert(value == 0x11 && starts == 1 && stops == 1 && pulse == 28);
    assert(wire_byte(1) == 0x55 && wire_byte(10) == 0x06);
    for (unsigned p = 19; p <= 27; ++p) assert(wire_released[p]);
    assert(now_us == 395); /* The reference's healthy read transition delays. */
    released();

    reset(false);
    assert(qn_bus_write_reg(0x2a, 0x02, 0x21) == QN_BUS_OK);
    assert(wire_byte(1) == 0x54 && wire_byte(10) == 0x02 && wire_byte(19) == 0x21);
    assert(starts == 1 && stops == 1 && pulse == 28 && now_us == 435);
    released();

    for (unsigned ack = 9; ack <= 27; ack += 9) {
        reset(false);
        nack_at = ack;
        assert(qn_bus_write_reg(0x28, 0x00, 0x05) == QN_BUS_NACK);
        assert(pulse == ack + 1 && stops == 1);
        released();
    }
    for (unsigned ack = 9; ack <= 18; ack += 9) {
        reset(true);
        nack_at = ack;
        value = 0xa5;
        assert(qn_bus_read_reg(0x21, 6, &value) == QN_BUS_NACK);
        assert(value == 0xa5 && stops == 1);
        released();
    }
    reset(true);
    stuck_sda = true;
    assert(qn_bus_read_reg(0x21, 6, &value) == QN_BUS_SDA_STUCK);
    released();
    reset(true);
    stuck_scl = true;
    assert(qn_bus_read_reg(0x21, 6, &value) == QN_BUS_SCL_TIMEOUT);
    released();
    reset(true);
    stuck_clock_at = 20; /* Clock fails while receiving data. */
    value = 0xa5;
    assert(qn_bus_read_reg(0x21, 6, &value) == QN_BUS_SCL_TIMEOUT);
    assert(value == 0xa5);
    released();
    reset(true);
    stretch_us = 900; /* Each edge alone is legal, total transaction is not. */
    assert(qn_bus_read_reg(0x21, 6, &value) == QN_BUS_TRANSACTION_TIMEOUT);
    released();
    reset(true);
    stuck_stop = true;
    value = 0xa5;
    assert(qn_bus_read_reg(0x21, 6, &value) == QN_BUS_SDA_STUCK);
    assert(value == 0xa5);
    released();
    reset(true);
    assert(qn_bus_read_reg(0x21, 6, NULL) == QN_BUS_INVALID_ARGUMENT);
    assert(qn_bus_write_reg(0x80, 0, 0) == QN_BUS_INVALID_ARGUMENT);
    assert(starts == 0);
    puts("QN bus: grammar, final NACK, timing, ACK failures, stuck lines and deadlines PASS");
}
