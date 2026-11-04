#include "agmen/net.hpp"

#include <cstring>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <mstcpip.h>
#else
#  include <arpa/inet.h>
#  include <cerrno>
#  include <netdb.h>
#  include <netinet/in.h>
#  include <poll.h>
#  include <sys/socket.h>
#  include <unistd.h>
#endif

namespace agmen::net {

namespace {

#ifdef _WIN32
using NativeSocket = SOCKET;
using LenType = int;  // Winsock recibe longitudes como int
constexpr NativeSocket kInvalid = INVALID_SOCKET;

int last_error() { return WSAGetLastError(); }
bool is_no_listener(int code) { return code == WSAECONNRESET || code == WSAECONNREFUSED; }
bool is_would_block(int code) { return code == WSAEWOULDBLOCK || code == WSAETIMEDOUT; }

// Winsock debe inicializarse una vez por proceso.
void ensure_winsock() {
    static const bool ready = [] {
        WSADATA data;
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
            throw NetError(WSAGetLastError(), "no se pudo inicializar Winsock");
        }
        return true;
    }();
    (void)ready;
}

#  ifndef SIO_UDP_CONNRESET
#    define SIO_UDP_CONNRESET _WSAIOW(IOC_VENDOR, 12)
#  endif
#else
using NativeSocket = int;
using LenType = std::size_t;  // POSIX recibe longitudes como size_t
constexpr NativeSocket kInvalid = -1;

int last_error() { return errno; }
bool is_no_listener(int code) { return code == ECONNREFUSED || code == ECONNRESET; }
bool is_would_block(int code) { return code == EAGAIN || code == EWOULDBLOCK || code == EINTR; }
void ensure_winsock() {}
#endif

NativeSocket native(std::intptr_t h) { return static_cast<NativeSocket>(h); }

std::string os_message(int code) { return std::system_category().message(code); }

/// "ip:puerto" -> sockaddr_in. Acepta nombres como "localhost".
sockaddr_in resolve(std::string_view address) {
    const auto colon = address.rfind(':');
    if (colon == std::string_view::npos) {
        throw NetError(0, "dirección inválida '" + std::string(address) +
                              "': se esperaba IP:PUERTO (ej. 127.0.0.1:7400)");
    }
    const std::string host(address.substr(0, colon));
    const std::string port(address.substr(colon + 1));

    addrinfo hints{};
    hints.ai_family = AF_INET;  // IPv4 por ahora; IPv6 en el nivel de red
    hints.ai_socktype = SOCK_DGRAM;
    addrinfo* result = nullptr;
    if (const int rc = getaddrinfo(host.c_str(), port.c_str(), &hints, &result); rc != 0) {
#ifdef _WIN32
        throw NetError(rc, "no se pudo resolver '" + std::string(address) + "'");
#else
        // En POSIX, getaddrinfo tiene sus propios códigos (no errno).
        throw NetError(0, "no se pudo resolver '" + std::string(address) + "' (" +
                              gai_strerror(rc) + ")");
#endif
    }
    sockaddr_in addr{};
    std::memcpy(&addr, result->ai_addr, sizeof addr);
    freeaddrinfo(result);
    return addr;
}

std::string to_string(const sockaddr_in& addr) {
    char ip[INET_ADDRSTRLEN] = "?";
    inet_ntop(AF_INET, &addr.sin_addr, ip, sizeof ip);
    return std::string(ip) + ":" + std::to_string(ntohs(addr.sin_port));
}

}  // namespace

Address Address::resolve(std::string_view address) {
    ensure_winsock();
    const sockaddr_in addr = net::resolve(address);
    static_assert(sizeof(sockaddr_in) == 16);
    Address a;
    std::memcpy(a.raw_.data(), &addr, sizeof addr);
    return a;
}

std::string Address::to_string() const {
    sockaddr_in addr{};
    std::memcpy(&addr, raw_.data(), sizeof addr);
    return net::to_string(addr);
}

NetError::NetError(int os_code, const std::string& what)
    : std::system_error(os_code, std::system_category(),
                        os_code != 0 ? what + " (" + os_message(os_code) + ")" : what) {}

UdpSocket UdpSocket::bind(std::string_view address) {
    ensure_winsock();
    const sockaddr_in addr = resolve(address);
    const NativeSocket s = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == kInvalid) {
        throw NetError(last_error(), "no se pudo crear el socket UDP");
    }
    UdpSocket sock(static_cast<std::intptr_t>(s));

#ifdef _WIN32
    // Sin esto, Windows hace fallar el siguiente recv con WSAECONNRESET
    // cuando un envío anterior llegó a un puerto sin nadie escuchando.
    BOOL report = FALSE;
    DWORD ignored = 0;
    WSAIoctl(s, SIO_UDP_CONNRESET, &report, sizeof report, nullptr, 0, &ignored, nullptr,
             nullptr);
#endif

    if (::bind(s, reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0) {
        const int code = last_error();
        throw NetError(code, "no se pudo escuchar en " + std::string(address) +
                                 ". ¿Hay otro proceso usando ese puerto?");
    }
    return sock;
}

UdpSocket::UdpSocket(UdpSocket&& other) noexcept : handle_(other.handle_) {
    other.handle_ = -1;
}

UdpSocket& UdpSocket::operator=(UdpSocket&& other) noexcept {
    if (this != &other) {
        close();
        handle_ = other.handle_;
        other.handle_ = -1;
    }
    return *this;
}

UdpSocket::~UdpSocket() { close(); }

void UdpSocket::close() noexcept {
    if (handle_ == -1) return;
#ifdef _WIN32
    ::closesocket(native(handle_));
#else
    ::close(native(handle_));
#endif
    handle_ = -1;
}

void UdpSocket::connect(std::string_view address) {
    const sockaddr_in addr = resolve(address);
    if (::connect(native(handle_), reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0) {
        throw NetError(last_error(), "no se pudo fijar el destino " + std::string(address));
    }
}

SendStatus UdpSocket::send(std::span<const std::uint8_t> data) {
    const auto n = ::send(native(handle_), reinterpret_cast<const char*>(data.data()),
                          static_cast<LenType>(data.size()), 0);
    if (n < 0) {
        const int code = last_error();
        if (is_no_listener(code)) return SendStatus::NoListener;
        throw NetError(code, "falló el envío UDP");
    }
    return SendStatus::Ok;
}

SendStatus UdpSocket::send_to(std::span<const std::uint8_t> data, const Address& to) {
    sockaddr_in addr{};
    std::memcpy(&addr, to.raw_.data(), sizeof addr);
    const auto n = ::sendto(native(handle_), reinterpret_cast<const char*>(data.data()),
                            static_cast<LenType>(data.size()), 0,
                            reinterpret_cast<const sockaddr*>(&addr), sizeof addr);
    if (n < 0) {
        const int code = last_error();
        if (is_no_listener(code) || is_would_block(code)) return SendStatus::NoListener;
        throw NetError(code, "falló el envío UDP a " + to.to_string());
    }
    return SendStatus::Ok;
}

std::uint16_t UdpSocket::local_port() const {
    sockaddr_in addr{};
    socklen_t len = sizeof addr;
    if (::getsockname(native(handle_), reinterpret_cast<sockaddr*>(&addr), &len) != 0) {
        throw NetError(last_error(), "no se pudo leer el puerto local");
    }
    return ntohs(addr.sin_port);
}

int wait_readable(std::span<const std::intptr_t> sockets, std::chrono::milliseconds timeout,
                  std::span<bool> ready) {
    for (auto& r : ready) r = false;
    if (sockets.empty()) {
        if (timeout.count() > 0) std::this_thread::sleep_for(timeout);
        return 0;
    }
#ifdef _WIN32
    std::vector<WSAPOLLFD> fds(sockets.size());
    for (std::size_t i = 0; i < sockets.size(); ++i) fds[i] = WSAPOLLFD{native(sockets[i]), POLLRDNORM, 0};
    const int n = WSAPoll(fds.data(), static_cast<ULONG>(fds.size()), static_cast<INT>(timeout.count()));
#else
    std::vector<pollfd> fds(sockets.size());
    for (std::size_t i = 0; i < sockets.size(); ++i) fds[i] = pollfd{native(sockets[i]), POLLIN, 0};
    const int n = ::poll(fds.data(), static_cast<nfds_t>(fds.size()), static_cast<int>(timeout.count()));
#endif
    if (n < 0) {
        const int code = last_error();
        if (is_would_block(code)) return 0;
        throw NetError(code, "falló la espera de datos");
    }
    int count = 0;
    for (std::size_t i = 0; i < sockets.size() && i < ready.size(); ++i) {
        ready[i] = fds[i].revents != 0;
        count += ready[i] ? 1 : 0;
    }
    return count;
}

std::optional<Received> UdpSocket::recv_from(std::span<std::uint8_t> buffer,
                                             std::chrono::milliseconds timeout) {
    const NativeSocket s = native(handle_);

    // Esperar con poll (WSAPoll en Windows): funciona igual en los tres sistemas.
#ifdef _WIN32
    WSAPOLLFD pfd{s, POLLRDNORM, 0};
    const int ready = WSAPoll(&pfd, 1, static_cast<INT>(timeout.count()));
#else
    pollfd pfd{s, POLLIN, 0};
    const int ready = ::poll(&pfd, 1, static_cast<int>(timeout.count()));
#endif
    if (ready == 0) return std::nullopt;  // timeout
    if (ready < 0) {
        const int code = last_error();
        if (is_would_block(code)) return std::nullopt;
        throw NetError(code, "falló la espera de datos");
    }

    sockaddr_in from{};
    socklen_t from_len = sizeof from;
    const auto n = ::recvfrom(s, reinterpret_cast<char*>(buffer.data()),
                              static_cast<LenType>(buffer.size()), 0,
                              reinterpret_cast<sockaddr*>(&from), &from_len);
    if (n < 0) {
        const int code = last_error();
        if (is_would_block(code) || is_no_listener(code)) return std::nullopt;
        throw NetError(code, "falló la recepción UDP");
    }
    return Received{static_cast<std::size_t>(n), to_string(from)};
}

}  // namespace agmen::net
