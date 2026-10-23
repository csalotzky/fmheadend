#pragma once
#include <algorithm>
#include <chrono>
#include <cstdint>

namespace muxd {
struct TimingDiagnostics {
    std::uint64_t late_max_us = 0, late_over_100us = 0, late_over_500us = 0,
                  late_over_1000us = 0, late_over_3000us = 0, send_gap_max_us = 0;
    void record_late(std::chrono::steady_clock::duration late) {
        using namespace std::chrono;
        late_max_us = std::max(late_max_us, static_cast<std::uint64_t>(duration_cast<microseconds>(late).count()));
        late_over_100us += late > microseconds(100);
        late_over_500us += late > microseconds(500);
        late_over_1000us += late > microseconds(1000);
        late_over_3000us += late > microseconds(3000);
    }
    void record_gap(std::chrono::steady_clock::duration gap) {
        send_gap_max_us = std::max(send_gap_max_us,
            static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(gap).count()));
    }
};
} // namespace muxd
