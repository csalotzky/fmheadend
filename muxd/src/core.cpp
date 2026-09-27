#include "muxd/core.hpp"
#include <algorithm>
#include <cstring>
#include <stdexcept>

namespace muxd {
std::size_t ring_capacity_bytes(std::uint64_t milliseconds) {
    constexpr auto bytes_per_ms = pcm_bytes_per_second / 1000;
    if (!milliseconds || milliseconds > std::vector<std::uint8_t>().max_size() / bytes_per_ms)
        throw std::invalid_argument("Ring buffer milliseconds must be positive and fit the byte container");
    return static_cast<std::size_t>(milliseconds) * bytes_per_ms;
}
RingBuffer::RingBuffer(std::size_t capacity_bytes) {
    if (!capacity_bytes || capacity_bytes % stereo_bytes)
        throw std::invalid_argument("Ring capacity must contain whole stereo frames");
    data_.resize(capacity_bytes);
}
void RingBuffer::push(const std::uint8_t* data, std::size_t size) {
    const auto ring_bytes = capacity();
    if (size % stereo_bytes) throw std::invalid_argument("PCM must contain whole stereo frames");
    if (size == 0) return;
    if (size > ring_bytes - size_) ++overruns_;
    if (size >= ring_bytes) {
        data += size - ring_bytes;
        size = ring_bytes;
        clear();
    } else if (size > ring_bytes - size_) {
        const auto discard = size - (ring_bytes - size_);
        head_ = (head_ + discard) % ring_bytes;
        size_ -= discard;
    }
    const auto tail = (head_ + size_) % ring_bytes;
    const auto first = std::min(size, ring_bytes - tail);
    std::memcpy(data_.data() + tail, data, first);
    std::memcpy(data_.data(), data + first, size - first);
    size_ += size;
}

std::size_t RingBuffer::pop(std::uint8_t* destination, std::size_t size) {
    const auto ring_bytes = capacity();
    if (size % stereo_bytes) throw std::invalid_argument("Read must contain whole stereo frames");
    const auto available = std::min(size, size_);
    const auto first = std::min(available, ring_bytes - head_);
    std::memcpy(destination, data_.data() + head_, first);
    std::memcpy(destination + first, data_.data(), available - first);
    if (available < size) {
        std::memset(destination + available, 0, size - available);
        ++underruns_;
    }
    head_ = (head_ + available) % ring_bytes;
    size_ -= available;
    return available;
}

void Channel::connect() {
    std::lock_guard<std::mutex> lock(mutex_);
    ring_.clear();
    partial_size_ = 0;
    connected_ = true;
}
void Channel::disconnect() {
    std::lock_guard<std::mutex> lock(mutex_);
    connected_ = false;
    ring_.clear();
    partial_size_ = 0; // Never join bytes from different producer sessions.
}
void Channel::feed(const std::uint8_t* data, std::size_t size) {
    std::lock_guard<std::mutex> lock(mutex_);
    received_ += size;
    if (partial_size_) {
        const auto take = std::min(size, stereo_bytes - partial_size_);
        std::memcpy(partial_.data() + partial_size_, data, take);
        partial_size_ += take;
        data += take;
        size -= take;
        if (partial_size_ == stereo_bytes) {
            ring_.push(partial_.data(), stereo_bytes);
            partial_size_ = 0;
        }
    }
    const auto whole = size - size % stereo_bytes;
    ring_.push(data, whole);
    if (size != whole) {
        partial_size_ = size - whole;
        std::memcpy(partial_.data(), data + whole, partial_size_);
    }
}
void Channel::read(std::uint8_t* destination) {
    std::lock_guard<std::mutex> lock(mutex_);
    consumed_ += ring_.pop(destination, channel_bytes);
}
ChannelStatus Channel::status() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return {connected_, ring_.size(), ring_.underruns(), ring_.overruns(), received_, consumed_};
}
Frame FrameBuilder::next(std::array<Channel, channels>& inputs) {
    Frame frame{};
    for (unsigned i = 0; i != 4; ++i) frame[i] = static_cast<std::uint8_t>(counter_ >> (8 * i));
    ++counter_; // Defined uint32 wraparound.
    for (std::size_t channel = 0; channel != channels; ++channel)
        inputs[channel].read(frame.data() + header_bytes + channel * channel_bytes);
    return frame;
}
} // namespace muxd
