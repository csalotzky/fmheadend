#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

namespace muxd {
constexpr std::size_t channels = 8;
constexpr std::size_t stereo_bytes = 4;
constexpr std::size_t samples_per_frame = 128;
constexpr std::size_t channel_bytes = samples_per_frame * stereo_bytes;
constexpr std::size_t header_bytes = 4;
constexpr std::size_t frame_bytes = header_bytes + channels * channel_bytes;
constexpr std::size_t sample_rate = 48000;
constexpr std::size_t pcm_bytes_per_second = sample_rate * stereo_bytes;
constexpr std::uint64_t default_ring_buffer_ms = 300;
std::size_t ring_capacity_bytes(std::uint64_t milliseconds);
using Frame = std::array<std::uint8_t, frame_bytes>;

// Single-owner ring. Channel synchronizes access from receiver/frame threads.
class RingBuffer {
public:
    explicit RingBuffer(std::size_t capacity_bytes = ring_capacity_bytes(default_ring_buffer_ms));
    std::size_t capacity() const noexcept { return data_.size(); }
    void push(const std::uint8_t* data, std::size_t size);
    std::size_t pop(std::uint8_t* destination, std::size_t size);
    void clear() noexcept { head_ = size_ = 0; }
    std::size_t size() const noexcept { return size_; }
    std::uint64_t underruns() const noexcept { return underruns_; }
    std::uint64_t overruns() const noexcept { return overruns_; }
private:
    std::vector<std::uint8_t> data_;
    std::size_t head_ = 0, size_ = 0;
    std::uint64_t underruns_ = 0, overruns_ = 0;
};

struct ChannelStatus {
    bool connected;
    std::size_t buffered_bytes;
    std::uint64_t underruns, overruns, bytes_received, bytes_consumed;
};

class Channel {
public:
    explicit Channel(std::size_t capacity_bytes = ring_capacity_bytes(default_ring_buffer_ms)) : ring_(capacity_bytes) {}
    void connect();
    void disconnect();
    void feed(const std::uint8_t* data, std::size_t size);
    void read(std::uint8_t* destination);
    ChannelStatus status() const;
private:
    mutable std::mutex mutex_;
    RingBuffer ring_;
    std::array<std::uint8_t, stereo_bytes> partial_{};
    std::size_t partial_size_ = 0;
    bool connected_ = false;
    std::uint64_t received_ = 0, consumed_ = 0;
};

class FrameBuilder {
public:
    explicit FrameBuilder(std::uint32_t counter = 0) : counter_(counter) {}
    Frame next(std::array<Channel, channels>& inputs);
private:
    std::uint32_t counter_;
};
} // namespace muxd
