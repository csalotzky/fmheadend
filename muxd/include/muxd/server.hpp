#pragma once
#include "muxd/transport.hpp"
#include <atomic>
#include <csignal>
#include <chrono>
#include <string>
#include <thread>
#include <vector>

namespace muxd {
class Server {
public:
    Server(std::string directory, Transport& transport,
           std::size_t ring_capacity = ring_capacity_bytes(default_ring_buffer_ms));
    ~Server();
    Server(const Server&) = delete;
    Server& operator=(const Server&) = delete;
    void run(const volatile std::sig_atomic_t& interrupted);
private:
    friend struct ServerTest;
    using Clock = std::chrono::steady_clock;
    enum class Command { SetCount, Pause, Resume };
    enum class StreamState { Waiting, Paused, Streaming };
    struct ControlPeer { int fd; std::string input, output; std::size_t sent = 0; Clock::time_point deadline; bool waiting = false; };
    // Protected by status_mutex_. Only the pump changes applied count/state.
    // Keep completed requests until their owner collects the result or disconnects.
    struct Pending {
        int owner = -1;
        Command command = Command::Pause;
        std::size_t count = 0;
        Clock::time_point deadline{};
        const char* result = nullptr;
    } pending_;
    StreamState stream_state_ = StreamState::Waiting;
    std::size_t tx_count_ = 0;
    std::string directory_;
    Transport& transport_;
    std::array<Channel, channels> inputs_;
    const int receive_buffer_bytes_;
    std::array<int, channels> listeners_, producers_;
    int control_ = -1, lock_ = -1;
    std::vector<std::string> paths_;
    std::vector<ControlPeer> peers_;
    std::atomic<bool> stopping_{false};
    std::thread audio_;
    mutable std::mutex status_mutex_;
    std::uint64_t frames_sent_ = 0, frames_built_ = 0, transport_errors_ = 0, late_frames_ = 0;
    bool transport_ok_ = false;
    std::string last_error_;
    int listen_socket(const std::string& name);
    void setup();
    void pump();
    void drop_producer(std::size_t channel);
    std::string status_json() const;
    const char* submit(int owner, Command command, std::size_t count, Clock::time_point deadline);
    void cancel(int owner);
    void apply_pending(); // Pump only, with status_mutex_ held, between transfers.
};
} // namespace muxd
