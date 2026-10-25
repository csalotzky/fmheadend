#pragma once
#include <algorithm>
#include <chrono>
#include <cstdint>

namespace muxd {
struct TimingSpan {
    std::uint64_t count=0, total_ns=0, min_us=0, max_us=0;
    void record(std::chrono::steady_clock::duration duration) {
        using namespace std::chrono;
        duration=std::max(duration,steady_clock::duration::zero());
        const auto us=static_cast<std::uint64_t>(duration_cast<microseconds>(duration).count());
        min_us=count ? std::min(min_us,us) : us;
        max_us=std::max(max_us,us);
        ++count;
        total_ns+=static_cast<std::uint64_t>(duration_cast<nanoseconds>(duration).count());
    }
};
struct TimingDiagnostics {
    TimingSpan send_call, wake_late;
    std::uint64_t rebase_total_ns=0;
    std::uint64_t late_max_us = 0, late_over_100us = 0, late_over_500us = 0,
                  late_over_1000us = 0, late_over_3000us = 0, send_gap_max_us = 0;
    void record_late(std::chrono::steady_clock::duration late) {
        using namespace std::chrono;
        rebase_total_ns+=static_cast<std::uint64_t>(duration_cast<nanoseconds>(late).count());
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
