#include "muxd/transport.hpp"
#include <algorithm>
#include <filesystem>
#include <iostream>
#include <iterator>
#include <stdexcept>

#define CHECK(c) do { if (!(c)) throw std::runtime_error("check failed: " #c); } while (false)
int main() {
    const auto path = std::filesystem::temp_directory_path() / "fmux-prefix-test.bin";
    try {
        std::array<muxd::Channel, muxd::channels> inputs;
        muxd::FrameBuilder builder(0x12345678);
        for (std::size_t channel = 0; channel < muxd::channels; ++channel) {
            std::array<std::uint8_t, muxd::channel_bytes> pcm{};
            pcm.fill(static_cast<std::uint8_t>(channel + 1));
            inputs[channel].connect();
            inputs[channel].feed(pcm.data(), pcm.size());
        }
        const auto frame = builder.next(inputs);
        CHECK(frame[0] == 0x78 && frame[1] == 0x56 && frame[2] == 0x34 && frame[3] == 0x12);
        for (std::size_t count = 1; count <= muxd::channels; ++count) {
            const auto bytes = muxd::spi_frame_bytes(count);
            {
                muxd::MockTransport transport(path.string(), 1);
                transport.send(frame, bytes);
            }
            std::ifstream file(path, std::ios::binary);
            const std::vector<std::uint8_t> captured((std::istreambuf_iterator<char>(file)), {});
            CHECK(captured.size() == bytes);
            CHECK(std::equal(captured.begin(), captured.end(), frame.begin()));
            for (std::size_t channel = 0; channel < count; ++channel)
                CHECK(std::all_of(captured.begin() + 4 + channel * muxd::channel_bytes,
                    captured.begin() + 4 + (channel + 1) * muxd::channel_bytes,
                    [channel](auto value) { return value == channel + 1; }));
            std::cout << "TX" << count << ": " << bytes << " bytes passed\n";
        }
        muxd::MockTransport transport("", 0);
        for (auto bytes : {std::size_t(0), std::size_t(4), muxd::spi_frame_bytes(1) + 1, muxd::frame_bytes + 512}) {
            bool rejected = false;
            try { transport.send(frame, bytes); }
            catch (const std::invalid_argument&) { rejected = true; }
            CHECK(rejected);
        }
        std::filesystem::remove(path);
    } catch (const std::exception& error) {
        std::filesystem::remove(path);
        std::cerr << error.what() << '\n'; return 1;
    }
}
