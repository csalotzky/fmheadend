#include "muxd/transport.hpp"
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <stdexcept>
#include <sys/ioctl.h>
#include <unistd.h>

namespace muxd {
MockTransport::MockTransport(const std::string& path, std::uint64_t count) : remaining_(count) {
    if (!path.empty()) {
        capture_.open(path, std::ios::binary | std::ios::trunc);
        if (!capture_) throw std::runtime_error("Cannot open mock capture file");
    }
}
void MockTransport::send(const Frame& frame) {
    if (capture_.is_open() && remaining_) {
        capture_.write(reinterpret_cast<const char*>(frame.data()), frame.size());
        if (!capture_) throw std::runtime_error("Mock capture write failed");
        if (--remaining_ == 0) capture_.close();
    }
}
SpidevTransport::SpidevTransport(const std::string& device, std::uint32_t clock_hz, std::uint8_t mode)
    : clock_hz_(clock_hz) {
    std::ifstream limit("/sys/module/spidev/parameters/bufsiz");
    std::size_t bytes = 0;
    if ((limit >> bytes) && bytes < frame_bytes)
        throw std::runtime_error("spidev bufsiz is below 4100; configure spidev.bufsiz=8192 before loading spidev");
    fd_ = ::open(device.c_str(), O_RDWR | O_CLOEXEC);
    if (fd_ < 0) throw std::runtime_error("Open spidev: " + std::string(std::strerror(errno)));
    std::uint8_t bits = 8, lsb = 0;
    if (ioctl(fd_, SPI_IOC_WR_MODE, &mode) < 0 || ioctl(fd_, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd_, SPI_IOC_WR_LSB_FIRST, &lsb) < 0 || ioctl(fd_, SPI_IOC_WR_MAX_SPEED_HZ, &clock_hz_) < 0) {
        const std::string error = std::strerror(errno);
        ::close(fd_); fd_ = -1;
        throw std::runtime_error("Configure spidev: " + error);
    }
}
SpidevTransport::~SpidevTransport() { if (fd_ >= 0) ::close(fd_); }
void SpidevTransport::send(const Frame& frame) {
    spi_ioc_transfer transfer{};
    transfer.tx_buf = reinterpret_cast<std::uintptr_t>(frame.data());
    transfer.len = frame.size();
    transfer.speed_hz = clock_hz_;
    transfer.bits_per_word = 8;
    const int result = ioctl(fd_, SPI_IOC_MESSAGE(1), &transfer);
    if (result != static_cast<int>(frame.size()))
        throw std::runtime_error(result < 0 ? std::string("SPI transfer: ") + std::strerror(errno) : "Short SPI transfer");
}
} // namespace muxd
