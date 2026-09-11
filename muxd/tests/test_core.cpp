#include "muxd/core.hpp"
#include <algorithm>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#define CHECK(condition) do { if (!(condition)) throw std::runtime_error("check failed: " #condition); } while (false)
using namespace muxd;

void ring_test() {
    RingBuffer ring;
    std::vector<std::uint8_t> input(ring_bytes, 0x71), out(ring_bytes);
    ring.push(input.data(), input.size());
    CHECK(ring.pop(out.data(), channel_bytes) == channel_bytes);
    std::vector<std::uint8_t> tail(channel_bytes, 0x29);
    ring.push(tail.data(), tail.size());
    CHECK(ring.pop(out.data(), out.size()) == ring_bytes);
    CHECK(std::all_of(out.begin(), out.end() - channel_bytes, [](auto v) { return v == 0x71; }));
    CHECK(std::equal(tail.begin(), tail.end(), out.end() - channel_bytes));
    CHECK(ring.overruns() == 0 && ring.underruns() == 0 && ring.size() == 0);
}
void underrun_test() {
    RingBuffer ring;
    std::array<std::uint8_t, channel_bytes> out{};
    out.fill(0xff);
    CHECK(ring.pop(out.data(), out.size()) == 0);
    CHECK(std::all_of(out.begin(), out.end(), [](auto v) { return v == 0; }));
    std::uint8_t stereo[]{0xff, 0x7f, 0x00, 0x80};
    ring.push(stereo, 4);
    CHECK(ring.pop(out.data(), out.size()) == 4);
    CHECK(std::equal(stereo, stereo + 4, out.begin()));
    CHECK(std::all_of(out.begin() + 4, out.end(), [](auto v) { return v == 0; }));
    CHECK(ring.underruns() == 2);
}
void overrun_test() {
    RingBuffer ring;
    std::vector<std::uint8_t> data(ring_bytes + 8), out(ring_bytes);
    for (std::size_t i = 0; i < data.size(); ++i) data[i] = static_cast<std::uint8_t>(i);
    ring.push(data.data(), ring_bytes);
    ring.push(data.data() + ring_bytes, 8);
    CHECK(ring.overruns() == 1);
    CHECK(ring.pop(out.data(), out.size()) == ring_bytes);
    CHECK(std::equal(out.begin(), out.end(), data.begin() + 8));
    ring.push(data.data(), data.size());
    CHECK(ring.overruns() == 2 && ring.size() == ring_bytes);
    ring.pop(out.data(), out.size());
    CHECK(std::equal(out.begin(), out.end(), data.begin() + 8));
}
void fragmented_test() {
    Channel channel;
    channel.connect();
    std::array<std::uint8_t, channel_bytes> data{}, out{};
    for (std::size_t i = 0; i < data.size(); ++i) data[i] = static_cast<std::uint8_t>(i);
    for (std::size_t i = 0; i < data.size(); ++i) channel.feed(data.data() + i, 1);
    channel.read(out.data());
    CHECK(out == data);
    CHECK(channel.status().bytes_received == channel_bytes);
    CHECK(channel.status().bytes_consumed == channel_bytes);
}
void frame_test() {
    std::array<Channel, channels> inputs;
    std::array<std::array<std::uint8_t, channel_bytes>, channels> expected{};
    for (std::size_t c = 0; c < channels; ++c) {
        for (std::size_t s = 0; s < samples_per_frame; ++s) {
            const auto left = static_cast<std::uint16_t>((c + 1) * 100 + s);
            const auto right = static_cast<std::uint16_t>(-static_cast<int>(left));
            expected[c][s * 4] = left & 255; expected[c][s * 4 + 1] = left >> 8;
            expected[c][s * 4 + 2] = right & 255; expected[c][s * 4 + 3] = right >> 8;
        }
        inputs[c].feed(expected[c].data(), channel_bytes);
    }
    FrameBuilder builder;
    const auto frame = builder.next(inputs);
    CHECK(frame.size() == 4100);
    for (std::size_t c = 0; c < channels; ++c)
        CHECK(std::equal(expected[c].begin(), expected[c].end(), frame.begin() + 4 + c * 512));
    const auto empty = builder.next(inputs);
    CHECK(std::all_of(empty.begin() + 4, empty.end(), [](auto value) { return value == 0; }));
}
void counter_test() {
    std::array<Channel, channels> inputs;
    FrameBuilder known(0x78563412);
    const auto frame = known.next(inputs);
    CHECK(frame[0] == 0x12 && frame[1] == 0x34 && frame[2] == 0x56 && frame[3] == 0x78);
    FrameBuilder wrap(0xffffffff);
    const auto last = wrap.next(inputs), first = wrap.next(inputs);
    CHECK(std::all_of(last.begin(), last.begin() + 4, [](auto v) { return v == 255; }));
    CHECK(std::all_of(first.begin(), first.begin() + 4, [](auto v) { return v == 0; }));
}
void disconnect_test() {
    Channel channel;
    channel.connect();
    const std::uint8_t old[]{1, 2, 3, 4, 5, 6, 7};
    channel.feed(old, 7);
    channel.disconnect();
    std::array<std::uint8_t, channel_bytes> out{};
    channel.read(out.data());
    CHECK(std::all_of(out.begin(), out.end(), [](auto v) { return v == 0; }));
    channel.connect();
    const std::uint8_t fresh[]{0xaa, 0xbb, 0xcc, 0xdd};
    channel.feed(fresh, 4); channel.read(out.data());
    CHECK(std::equal(fresh, fresh + 4, out.begin()));
    CHECK(std::all_of(out.begin() + 4, out.end(), [](auto v) { return v == 0; }));
}
int main(int argc, char** argv) {
    try {
        const std::string name = argc > 1 ? argv[1] : "";
        if (name == "ring") ring_test(); else if (name == "underrun") underrun_test();
        else if (name == "overrun") overrun_test(); else if (name == "fragmented") fragmented_test();
        else if (name == "frame") frame_test(); else if (name == "counter") counter_test();
        else if (name == "disconnect") disconnect_test(); else throw std::runtime_error("unknown test");
        std::cout << name << " passed\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
