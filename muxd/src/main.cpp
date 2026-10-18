#include <csignal>
#include "muxd/server.hpp"
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>

namespace {
volatile std::sig_atomic_t interrupted = 0;
void stop(int) { interrupted = 1; }
std::uint64_t number(const std::string& text, std::uint64_t maximum) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos) throw std::runtime_error("Invalid numeric option");
    std::size_t used = 0;
    const auto value = std::stoull(text, &used);
    if (used != text.size() || value > maximum) throw std::runtime_error("Numeric option out of range");
    return value;
}
}
int main(int argc, char** argv) {
    try {
        std::string directory = "/run/fmheadend", backend, device = "/dev/spidev0.0", capture;
        std::uint32_t clock = 25000000;
        std::uint8_t mode = 0;
        std::uint64_t capture_frames = 375;
        std::uint64_t ring_buffer_ms = muxd::default_ring_buffer_ms;
        for (int i = 1; i < argc; ++i) {
            const std::string option = argv[i];
            if (option == "--version") {
                std::cout << "fmheadend-muxd " << FMHEADEND_MUXD_VERSION << '\n';
                return 0;
            }
            if (option == "--help") {
                std::cout << "fmheadend-muxd --transport mock|spidev [--runtime-dir /run/fmheadend]\n"
                             "  [--spi-device /dev/spidev0.0] [--spi-clock-hz 25000000] [--spi-mode 0]\n"
                             "  [--ring-buffer-ms 300] (positive integer ms of PCM per channel)\n"
                             "  [--capture FILE --capture-frames 375] (bounded mock capture only)\n"
                             "  --version\n";
                return 0;
            }
            if (++i == argc) throw std::runtime_error("Missing option value");
            const std::string value = argv[i];
            if (option == "--runtime-dir") directory = value;
            else if (option == "--transport") backend = value;
            else if (option == "--spi-device") device = value;
            else if (option == "--spi-clock-hz") clock = number(value, std::numeric_limits<std::uint32_t>::max());
            else if (option == "--spi-mode") mode = number(value, 3);
            else if (option == "--capture") capture = value;
            else if (option == "--capture-frames") capture_frames = number(value, 100000);
            else if (option == "--ring-buffer-ms") ring_buffer_ms = number(value, std::numeric_limits<std::uint64_t>::max());
            else throw std::runtime_error("Unknown option: " + option);
        }
        if (!clock || !capture_frames) throw std::runtime_error("Clock and capture limit must be positive");
        const auto ring_capacity = muxd::ring_capacity_bytes(ring_buffer_ms);
        std::unique_ptr<muxd::Transport> transport;
        if (backend == "mock") transport = std::make_unique<muxd::MockTransport>(capture, capture_frames);
        else if (backend == "spidev") {
            if (!capture.empty()) throw std::runtime_error("Capture is only available with mock transport");
            transport = std::make_unique<muxd::SpidevTransport>(device, clock, mode);
        } else throw std::runtime_error("Select --transport mock or --transport spidev explicitly");
        std::signal(SIGINT, stop); std::signal(SIGTERM, stop); std::signal(SIGPIPE, SIG_IGN);
        std::cerr << "fmheadend-muxd transport=" << backend << " runtime=" << directory
                  << " ring_buffer_ms=" << ring_buffer_ms << " ring_buffer_bytes=" << ring_capacity << '\n';
        muxd::Server server(directory, *transport, ring_capacity);
        server.run(interrupted);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "fmheadend-muxd: " << error.what() << '\n';
        return 1;
    }
}
