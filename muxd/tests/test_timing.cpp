#include "muxd/timing.hpp"
#include <stdexcept>

#define CHECK(c) do { if (!(c)) throw std::runtime_error("check failed: " #c); } while (false)
using namespace std::chrono_literals;

int main() {
    muxd::TimingDiagnostics timing;
    CHECK(timing.late_max_us == 0 && timing.send_gap_max_us == 0);
    timing.record_late(100us);
    CHECK(timing.late_max_us == 100 && timing.late_over_100us == 0);
    timing.record_late(100us + 1ns);
    timing.record_late(500us);
    timing.record_late(1000us);
    timing.record_late(3000us);
    CHECK(timing.late_over_100us == 4 && timing.late_over_500us == 2);
    CHECK(timing.late_over_1000us == 1 && timing.late_over_3000us == 0);
    timing.record_late(3001us);
    timing.record_late(1us);
    CHECK(timing.late_max_us == 3001 && timing.late_over_3000us == 1);
    timing.record_gap(2666us + 999ns);
    timing.record_gap(5000us);
    timing.record_gap(100us);
    CHECK(timing.send_gap_max_us == 5000);
}
