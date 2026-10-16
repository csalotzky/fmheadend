#pragma once
#include "muxd/core.hpp"
#include <fstream>
#include <string>

namespace muxd {
class Transport {
public:
    virtual ~Transport() = default;
    virtual void send(const Frame& frame, std::size_t bytes) = 0;
    virtual bool mocked() const noexcept = 0;
};
class MockTransport final : public Transport {
public:
    MockTransport(const std::string& capture_path, std::uint64_t capture_frames);
    void send(const Frame& frame, std::size_t bytes) override;
    bool mocked() const noexcept override { return true; }
private:
    std::ofstream capture_;
    std::uint64_t remaining_;
};
class SpidevTransport final : public Transport {
public:
    SpidevTransport(const std::string& device, std::uint32_t clock_hz, std::uint8_t mode);
    ~SpidevTransport() override;
    void send(const Frame& frame, std::size_t bytes) override;
    bool mocked() const noexcept override { return false; }
private:
    int fd_ = -1;
    std::uint32_t clock_hz_;
};
} // namespace muxd
