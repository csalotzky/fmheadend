#include "muxd/server.hpp"
#include <condition_variable>
#include <iostream>
#include <stdexcept>

#define CHECK(c) do { if (!(c)) throw std::runtime_error("check failed: " #c); } while (false)
using namespace std::chrono_literals;

// Mock-only transfer barrier: the test controls completion of one synchronous send.
class BlockingMockTransport final : public muxd::Transport {
public:
    std::mutex mutex;
    std::condition_variable changed;
    bool block = true, entered = false;
    void send(const muxd::Frame&, std::size_t bytes) override {
        CHECK(bytes == muxd::spi_frame_bytes(4));
        std::unique_lock<std::mutex> lock(mutex);
        entered = true;
        changed.notify_all();
        CHECK(changed.wait_for(lock, 2s, [&] { return !block; }));
    }
    bool mocked() const noexcept override { return true; }
    void wait_entered() {
        std::unique_lock<std::mutex> lock(mutex);
        CHECK(changed.wait_for(lock, 1s, [&] { return entered; }));
    }
    void release() {
        std::lock_guard<std::mutex> lock(mutex);
        block = false;
        changed.notify_all();
    }
    void arm() {
        std::lock_guard<std::mutex> lock(mutex);
        block = true;
        entered = false;
    }
};

namespace muxd {
struct ServerTest {
    static const char* result(Server& server) {
        std::lock_guard<std::mutex> lock(server.status_mutex_);
        return server.pending_.result;
    }
    static void completed(Server& server, const char* expected) {
        const auto deadline = Server::Clock::now() + 1s;
        while (!result(server) && Server::Clock::now() < deadline) std::this_thread::sleep_for(1ms);
        CHECK(result(server));
        CHECK(std::string(result(server)).find(expected) != std::string::npos);
        server.cancel(1);
    }
    static void run() {
        BlockingMockTransport transport;
        Server server("", transport);
        server.audio_ = std::thread([&] { server.pump(); });
        const auto request = [&](Server::Command command, int count = 0, std::chrono::milliseconds ttl = 500ms) {
            return server.submit(1, command, count, Server::Clock::now() + ttl);
        };
        std::this_thread::sleep_for(15ms);
        CHECK(server.status_json().find("\"frames_sent\":0") != std::string::npos);
        CHECK(!request(Server::Command::SetCount, 4));
        transport.wait_entered();
        completed(server, "true");
        // No control lock is held by the blocked synchronous transfer.
        CHECK(request(Server::Command::SetCount, 4));
        CHECK(std::string(request(Server::Command::SetCount, 8)).find("INVALID_STATE") != std::string::npos);
        CHECK(!request(Server::Command::Pause));
        std::this_thread::sleep_for(20ms);
        CHECK(!result(server)); // Pause cannot ACK the in-progress transfer.
        transport.release();
        completed(server, "true");
        CHECK(server.status_json().find("\"stream_state\":\"PAUSED\"") != std::string::npos);

        transport.arm();
        CHECK(!request(Server::Command::Resume));
        transport.wait_entered();
        completed(server, "true");
        CHECK(!request(Server::Command::Pause, 0, 10ms));
        std::this_thread::sleep_for(25ms);
        transport.release();
        completed(server, "CONTROL_TIMEOUT");
        CHECK(server.status_json().find("\"stream_state\":\"STREAMING\"") != std::string::npos);

        transport.arm();
        transport.wait_entered();
        CHECK(!request(Server::Command::Pause));
        server.cancel(1); // Disconnected/timed-out owner: no delayed application.
        transport.release();
        std::this_thread::sleep_for(15ms);
        CHECK(server.status_json().find("\"stream_state\":\"STREAMING\"") != std::string::npos);
    }
};
}
int main() {
    try { muxd::ServerTest::run(); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
