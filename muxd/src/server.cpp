#include <csignal>
#include "muxd/server.hpp"
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <poll.h>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <utility>
#include <limits>

namespace muxd {
namespace {
template<std::size_t... I>
std::array<Channel, channels> make_channels(std::size_t capacity, std::index_sequence<I...>) {
    return {{Channel((static_cast<void>(I), capacity))...}};
}
void close_fd(int& fd) { if (fd >= 0) { ::close(fd); fd = -1; } }
std::runtime_error system_error(const char* operation) {
    return std::runtime_error(std::string(operation) + ": " + std::strerror(errno));
}
std::string quote(const std::string& text) {
    std::string result = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') { result += '\\'; result += c; }
        else if (c < 32) { result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15]; }
        else result += c;
    }
    return result + '"';
}
constexpr const char* success = "{\"ok\":true}\n";
constexpr const char* state_error = "{\"error\":\"INVALID_STATE\",\"message\":\"Configure before resume; pause before changing TX count\"}\n";
constexpr const char* timeout_error = "{\"error\":\"CONTROL_TIMEOUT\",\"message\":\"Command expired without application\"}\n";
}
Server::Server(std::string directory, Transport& transport, std::size_t ring_capacity, bool timing_diagnostics)
    : directory_(std::move(directory)), transport_(transport),
      inputs_(make_channels(ring_capacity, std::make_index_sequence<channels>{})),
      receive_buffer_bytes_(static_cast<int>(std::min<std::size_t>(ring_capacity, std::numeric_limits<int>::max()))),
      timing_diagnostics_(timing_diagnostics) {
    listeners_.fill(-1); producers_.fill(-1);
}
Server::~Server() {
    stopping_ = true;
    if (audio_.joinable()) audio_.join();
    for (auto& fd : producers_) close_fd(fd);
    for (auto& fd : listeners_) close_fd(fd);
    for (auto& peer : peers_) close_fd(peer.fd);
    close_fd(control_);
    for (const auto& path : paths_) ::unlink(path.c_str());
    close_fd(lock_);
}
int Server::listen_socket(const std::string& name) {
    const auto path = directory_ + "/" + name;
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (path.size() >= sizeof(address.sun_path)) throw std::runtime_error("Unix socket path is too long");
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    struct stat info{};
    if (::lstat(path.c_str(), &info) == 0) {
        if (!S_ISSOCK(info.st_mode)) throw std::runtime_error("Refusing to replace non-socket: " + path);
        if (::unlink(path.c_str()) < 0) throw system_error("unlink stale socket");
    } else if (errno != ENOENT) throw system_error("lstat socket");
    int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) throw system_error("socket");
    if (::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0) {
        const auto error = system_error("bind"); close_fd(fd); throw error;
    }
    paths_.push_back(path);
    if (::listen(fd, 8) < 0) { const auto error = system_error("listen"); close_fd(fd); throw error; }
    return fd;
}
void Server::setup() {
    ::umask(0007);
    std::filesystem::create_directories(directory_);
    lock_ = ::open((directory_ + "/.muxd.lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0660);
    if (lock_ < 0) throw system_error("open runtime lock");
    if (::flock(lock_, LOCK_EX | LOCK_NB) < 0) throw system_error("another mux daemon owns the runtime directory");
    for (std::size_t i = 0; i != channels; ++i) listeners_[i] = listen_socket("pcm" + std::to_string(i) + ".sock");
    control_ = listen_socket("mux-control.sock");
}
void Server::drop_producer(std::size_t channel) {
    close_fd(producers_[channel]);
    inputs_[channel].disconnect();
}
const char* Server::submit(int owner, Command command, std::size_t count, Clock::time_point deadline) {
    std::lock_guard<std::mutex> lock(status_mutex_);
    if (pending_.owner != -1)
        return "{\"error\":\"CONTROL_BUSY\"}\n";
    if (command == Command::SetCount && (count < 1 || count > channels))
        return "{\"error\":\"INVALID_COUNT\"}\n";
    if ((command == Command::Resume && tx_count_ == 0) ||
        (command == Command::SetCount && stream_state_ == StreamState::Streaming && count != tx_count_))
        return state_error;
    if ((command == Command::SetCount && count == tx_count_) ||
        (command == Command::Resume && stream_state_ == StreamState::Streaming) ||
        (command == Command::Pause && stream_state_ != StreamState::Streaming))
        return success;
    pending_ = {owner, command, count, deadline, nullptr};
    return nullptr;
}
void Server::cancel(int owner) {
    std::lock_guard<std::mutex> lock(status_mutex_);
    if (pending_.owner == owner) pending_ = {};
}
void Server::apply_pending() {
    if (pending_.owner == -1 || pending_.result) return;
    if (Clock::now() >= pending_.deadline) { pending_.result = timeout_error; return; }
    switch (pending_.command) {
    case Command::SetCount:
        tx_count_ = pending_.count;
        if (stream_state_ == StreamState::Waiting) stream_state_ = StreamState::Streaming;
        break;
    case Command::Pause: stream_state_ = StreamState::Paused; break;
    case Command::Resume: stream_state_ = StreamState::Streaming; break;
    }
    pending_.result = success;
}
void Server::pump() {
    FrameBuilder builder;
    auto deadline = Clock::now();
    // Carry fractional nanoseconds without changing the PCM frame cadence.
    std::uint64_t remainder = 0;
    Clock::time_point woke{}, sleep_deadline{};
    bool pending_timing = false;
    Clock::time_point previous_send{};
    bool have_previous_send = false;
    while (!stopping_) {
        Frame frame = builder.next(inputs_);
        std::size_t bytes = 0;
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            apply_pending();
            if (stream_state_ == StreamState::Streaming) bytes = spi_frame_bytes(tx_count_);
        }
        bool ok = true;
        std::string error;
        Clock::time_point send_start{};
        try {
            if (bytes) {
                if (timing_diagnostics_) send_start = Clock::now();
                transport_.send(frame, bytes);
            }
        }
        catch (const std::exception& exc) { ok = false; error = exc.what(); }
        const auto completed = timing_diagnostics_ && bytes ? Clock::now() : Clock::time_point{};
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            if (timing_diagnostics_ && pending_timing) {
                timing_.wake_late.record(woke-sleep_deadline);
            }
            ++frames_built_;
            if (bytes) {
                if (timing_diagnostics_) {
                    timing_.send_call.record(completed-send_start);
                    if (have_previous_send) timing_.record_gap(send_start - previous_send);
                    previous_send = send_start;
                    have_previous_send = true;
                }
                if (ok) ++frames_sent_; else ++transport_errors_;
                transport_ok_ = ok;
                last_error_ = std::move(error);
            }
        }
        remainder += samples_per_frame * 1000000000ULL;
        deadline += std::chrono::nanoseconds(remainder / sample_rate);
        remainder %= sample_rate;
        const auto now = Clock::now();
        if (deadline < now) {
            // Do not burst old frames after a scheduling/transport stall.
            const auto late = timing_diagnostics_ ? now - deadline : Clock::duration::zero();
            deadline = now;
            std::lock_guard<std::mutex> lock(status_mutex_);
            ++late_frames_;
            if (timing_diagnostics_) timing_.record_late(late);
        }
        pending_timing = timing_diagnostics_ && bytes;
        if (pending_timing) {
            sleep_deadline=deadline;
        }
        std::this_thread::sleep_until(deadline);
        if (pending_timing) woke=Clock::now();
    }
}
std::string Server::status_json() const {
    std::ostringstream out;
    {
        std::lock_guard<std::mutex> lock(status_mutex_);
        out << "{\"state\":\"RUNNING\",\"transport\":" << quote(transport_.mocked() ? "mock" : "spidev")
            << ",\"mocked\":" << (transport_.mocked() ? "true" : "false")
            << ",\"transport_state\":" << quote(transport_ok_ ? "RUNNING" : frames_sent_ + transport_errors_ ? "ERROR" : "STARTING")
            << ",\"tx_count\":" << (tx_count_ ? std::to_string(tx_count_) : "null")
            << ",\"spi_frame_bytes\":" << (tx_count_ ? std::to_string(spi_frame_bytes(tx_count_)) : "null")
            << ",\"stream_state\":" << quote(stream_state_ == StreamState::Waiting ? "WAITING_FOR_CONFIG" :
                stream_state_ == StreamState::Paused ? "PAUSED" : "STREAMING")
            << ",\"spi_ok\":" << (transport_.mocked() ? "null" : transport_ok_ ? "true" : "false")
            << ",\"frames_sent\":" << frames_sent_ << ",\"frames_built\":" << frames_built_
            << ",\"transport_errors\":" << transport_errors_
            << ",\"spi_errors\":" << (transport_.mocked() ? 0 : transport_errors_)
            << ",\"late_frames\":" << late_frames_
            << ",\"timing_diagnostics\":" << (timing_diagnostics_ ? "true" : "false")
            << ",\"late_max_us\":" << timing_.late_max_us
            << ",\"late_over_100us\":" << timing_.late_over_100us
            << ",\"late_over_500us\":" << timing_.late_over_500us
            << ",\"late_over_1000us\":" << timing_.late_over_1000us
            << ",\"late_over_3000us\":" << timing_.late_over_3000us
            << ",\"send_gap_max_us\":" << timing_.send_gap_max_us
            << ",\"rebase_total_ns\":" << timing_.rebase_total_ns
            << ",\"timing_stages\":{";
        bool first=true;
        const auto span = [&](const char* name, const TimingSpan& value) {
            if (!first) out << ',';
            first=false;
            out << quote(name) << ":{\"count\":" << value.count
                << ",\"total_ns\":" << value.total_ns << ",\"min_us\":" << value.min_us
                << ",\"max_us\":" << value.max_us << '}';
        };
        span("send_call",timing_.send_call); span("wake_late",timing_.wake_late);
        out << "},\"last_error\":" << (last_error_.empty() ? "null" : quote(last_error_)) << ",\"channels\":[";
    }
    for (std::size_t i = 0; i != channels; ++i) {
        const auto item = inputs_[i].status();
        if (i) out << ',';
        out << "{\"id\":" << i << ",\"connected\":" << (item.connected ? "true" : "false")
            << ",\"buffered_bytes\":" << item.buffered_bytes << ",\"underruns\":" << item.underruns
            << ",\"overruns\":" << item.overruns << ",\"bytes_received\":" << item.bytes_received
            << ",\"bytes_consumed\":" << item.bytes_consumed << '}';
    }
    out << "]}\n";
    return out.str();
}
void Server::run(const volatile std::sig_atomic_t& interrupted) {
    setup();
    audio_ = std::thread(&Server::pump, this);
    const std::regex request(R"re(^\s*\{\s*"command"\s*:\s*"(get_status|pause|resume)"\s*\}\s*$)re");
    const std::regex set_count(R"re(^\s*\{\s*"command"\s*:\s*"set_tx_count"\s*,\s*"count"\s*:\s*([1-8])\s*\}\s*$)re");
    const std::regex count_first(R"re(^\s*\{\s*"count"\s*:\s*([1-8])\s*,\s*"command"\s*:\s*"set_tx_count"\s*\}\s*$)re");
    while (!interrupted) {
        std::vector<pollfd> fds;
        fds.reserve(17 + peers_.size());
        for (const auto fd : listeners_) fds.push_back({fd, POLLIN, 0});
        for (const auto fd : producers_) fds.push_back({fd, POLLIN, 0});
        fds.push_back({control_, POLLIN, 0});
        const auto peer_count = peers_.size();
        for (const auto& peer : peers_) fds.push_back({peer.fd, static_cast<short>(peer.output.empty() ? POLLIN : POLLOUT), 0});
        const int ready = ::poll(fds.data(), fds.size(), 20);
        if (ready < 0 && errno != EINTR) throw system_error("poll");
        for (std::size_t i = 0; i != channels; ++i) {
            // Read old producer before accepting a replacement, including EOF.
            if (producers_[i] >= 0 && fds[channels + i].revents) {
                std::array<std::uint8_t, 8192> data{};
                for (int budget = 0; budget != 4; ++budget) {
                    const auto count = ::recv(producers_[i], data.data(), data.size(), 0);
                    if (count > 0) inputs_[i].feed(data.data(), static_cast<std::size_t>(count));
                    else if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                        drop_producer(i); break;
                    } else break;
                }
            }
            if (fds[i].revents & POLLIN) {
                int incoming = ::accept4(listeners_[i], nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
                if (incoming >= 0) {
                    if (producers_[i] >= 0) close_fd(incoming); // One producer per channel.
                    else {
                        // SO_RCVBUF takes int; the kernel also applies its own cap.
                        const int bytes = receive_buffer_bytes_;
                        ::setsockopt(incoming, SOL_SOCKET, SO_RCVBUF, &bytes, sizeof(bytes));
                        producers_[i] = incoming; inputs_[i].connect();
                    }
                }
            }
        }
        for (std::size_t i = 0; i != peer_count; ++i) {
            auto& peer = peers_[i];
            if (peer.waiting) {
                std::lock_guard<std::mutex> lock(status_mutex_);
                if (pending_.owner == peer.fd && pending_.result) {
                    peer.output = pending_.result;
                    pending_ = {};
                    peer.waiting = false;
                    // Application has completed; allow a bounded reply-delivery window.
                    peer.deadline = Clock::now() + std::chrono::seconds(2);
                }
            }
            if (Clock::now() >= peer.deadline) { cancel(peer.fd); close_fd(peer.fd); continue; }
            const auto events = fds[17 + i].revents;
            if (peer.waiting) {
                if (events & (POLLIN | POLLHUP | POLLERR)) { cancel(peer.fd); close_fd(peer.fd); }
                continue;
            }
            if (peer.output.empty() && (events & (POLLIN | POLLHUP | POLLERR))) {
                char data[1024];
                const auto count = ::recv(peer.fd, data, sizeof(data), 0);
                if (count <= 0) { if (count == 0 || errno != EAGAIN) close_fd(peer.fd); continue; }
                peer.input.append(data, static_cast<std::size_t>(count));
                if (peer.input.size() > 1024) { close_fd(peer.fd); continue; }
                if (peer.input.find('\n') != std::string::npos) {
                    std::smatch match;
                    if (std::regex_match(peer.input, match, request)) {
                        const auto command = match[1].str();
                        if (command == "get_status") peer.output = status_json();
                        else {
                            const auto result = submit(peer.fd, command == "pause" ? Command::Pause : Command::Resume, 0, peer.deadline);
                            if (result) peer.output = result; else peer.waiting = true;
                        }
                    } else if (std::regex_match(peer.input, match, set_count) || std::regex_match(peer.input, match, count_first)) {
                        const auto result = submit(peer.fd, Command::SetCount, match[1].str()[0] - '0', peer.deadline);
                        if (result) peer.output = result; else peer.waiting = true;
                    } else peer.output = "{\"error\":\"INVALID_COMMAND\",\"message\":\"Expected get_status, pause, resume or set_tx_count with integer count 1..8\"}\n";
                }
            } else if (!peer.output.empty() && (events & (POLLOUT | POLLHUP | POLLERR))) {
                const auto count = ::send(peer.fd, peer.output.data() + peer.sent, peer.output.size() - peer.sent, MSG_NOSIGNAL);
                if (count > 0) peer.sent += static_cast<std::size_t>(count);
                else if (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) close_fd(peer.fd);
                if (peer.sent == peer.output.size()) close_fd(peer.fd);
            }
        }
        peers_.erase(std::remove_if(peers_.begin(), peers_.end(), [](const auto& p) { return p.fd < 0; }), peers_.end());
        if (fds[16].revents & POLLIN) {
            int incoming = ::accept4(control_, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (incoming >= 0) {
                if (peers_.size() >= 16) close_fd(incoming);
                else peers_.push_back({incoming, {}, {}, 0, Clock::now() + std::chrono::seconds(2)});
            }
        }
    }
    {
        std::lock_guard<std::mutex> lock(status_mutex_);
        pending_ = {};
    }
    stopping_ = true;
}
} // namespace muxd
