#include "agmen/net.hpp"

#include "agmen/log.hpp"

#include <atomic>
#include <cstring>
#include <mutex>
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
#  include <fcntl.h>
#  include <netdb.h>
#  include <netinet/in.h>
#  include <netinet/tcp.h>
#  include <poll.h>
#  include <pthread.h>
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

// ---------------------------------------------------------------------------
// El registro de los sockets abiertos (solo POSIX), para el hijo de un fork (PR "fork"): el binding de
// Python los tapa todos con /dev/null en el hijo (detail::cover_inherited_sockets), sin recorrer los
// objetos heredados ni tomar sus candados. Cuesta un candado al abrir y otro al cerrar un socket; nada
// por mensaje. Un programa de C++ que haga fork queda como antes: nadie llama a esa función.
// Límite conocido: entre abrir un socket y anotarlo (o entre quitarlo y cerrarlo) hay ventanas de
// microsegundos sin el candado; si el fork cae justo ahí, el hijo se queda con ese socket abierto (no se
// tapa). Nunca se tapa un descriptor ajeno: solo los números que están en el registro.
// ---------------------------------------------------------------------------

std::atomic<unsigned> g_fork_generation{0};

#ifndef _WIN32
struct Registry {
    std::mutex mu;
    std::vector<int> fds;
};

Registry* g_registry = nullptr;

// pthread_atfork: el padre toma el candado del registro antes del fork y lo suelta después, en los dos
// procesos. Así el hijo lo hereda libre y con la lista entera (ningún hilo la dejó a medio cambiar). El
// gancho del hijo solo suelta el candado: no reserva memoria ni toma otros.
void registry_before_fork() { g_registry->mu.lock(); }
void registry_after_fork() { g_registry->mu.unlock(); }

Registry& registry() {
    // Nunca se destruye: un socket se puede cerrar al salir del programa, después de los estáticos.
    static Registry* const r = [] {
        g_registry = new Registry();
        ::pthread_atfork(&registry_before_fork, &registry_after_fork, &registry_after_fork);
        return g_registry;
    }();
    return *r;
}

void note_open(std::intptr_t h) noexcept {
    if (h == -1) return;
    try {
        Registry& r = registry();
        const std::lock_guard lock(r.mu);
        r.fds.push_back(static_cast<int>(h));
    } catch (...) {
        // sin memoria: este socket no se tapa en el hijo de un fork (queda abierto ahí, como antes)
    }
}

void note_close(std::intptr_t h) noexcept {
    if (h == -1) return;
    Registry& r = registry();
    const std::lock_guard lock(r.mu);
    for (auto& fd : r.fds) {
        if (fd == static_cast<int>(h)) {
            fd = r.fds.back();
            r.fds.pop_back();
            return;
        }
    }
}
#else
void note_open(std::intptr_t) noexcept {}
void note_close(std::intptr_t) noexcept {}
#endif

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

    // macOS no deja mandar un datagrama de más de 9216 bytes (net.inet.udp.maxdgram)
    // si el socket no pide más espacio de envío; un f32[3000] fallaría con "Message
    // too long" (RFC 0008 §1.2). Se pide lo que cabe en un datagrama en todos los
    // sistemas: así un mensaje compact de 64 KB sale igual en Windows, Linux y macOS.
    {
        const int want = 65507;
        int have = 0;
        socklen_t len = sizeof have;
        if (::getsockopt(s, SOL_SOCKET, SO_SNDBUF, reinterpret_cast<char*>(&have), &len) != 0 || have < want) {
            ::setsockopt(s, SOL_SOCKET, SO_SNDBUF, reinterpret_cast<const char*>(&want), sizeof want);
        }
    }

    if (::bind(s, reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0) {
        const int code = last_error();
        throw NetError(code, "no se pudo escuchar en " + std::string(address) +
                                 ". ¿Hay otro proceso usando ese puerto?");
    }
    return sock;
}

UdpSocket::UdpSocket(std::intptr_t handle) : handle_(handle) { note_open(handle_); }

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
    note_close(handle_);   // antes de cerrar: después, otro hilo puede abrir uno con el mismo número
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

    // Sin bloquear: entre el poll y la lectura, otro lector del mismo socket (el
    // hijo de un fork lo comparte) puede llevarse el datagrama; esperar ahí no
    // tendría límite. Windows no tiene fork ni MSG_DONTWAIT.
#ifdef _WIN32
    constexpr int kFlags = 0;
#else
    constexpr int kFlags = MSG_DONTWAIT;
#endif
    sockaddr_in from{};
    socklen_t from_len = sizeof from;
    const auto n = ::recvfrom(s, reinterpret_cast<char*>(buffer.data()),
                              static_cast<LenType>(buffer.size()), kFlags,
                              reinterpret_cast<sockaddr*>(&from), &from_len);
    if (n < 0) {
        const int code = last_error();
        if (is_would_block(code) || is_no_listener(code)) return std::nullopt;
        throw NetError(code, "falló la recepción UDP");
    }
    return Received{static_cast<std::size_t>(n), to_string(from)};
}

}  // namespace agmen::net

// ---------------------------------------------------------------------------
// TCP (RFC 0008)
// ---------------------------------------------------------------------------

namespace agmen::net {

namespace {

void close_native(std::intptr_t h) noexcept {
    note_close(h);   // antes de cerrar (ver UdpSocket::close)
#ifdef _WIN32
    ::closesocket(native(h));
#else
    ::close(native(h));
#endif
}

void set_nonblocking(NativeSocket s) {
#ifdef _WIN32
    u_long on = 1;
    ::ioctlsocket(s, FIONBIO, &on);
#else
    const int flags = ::fcntl(s, F_GETFL, 0);
    ::fcntl(s, F_SETFL, flags | O_NONBLOCK);
#endif
}

/// Un buffer grande para que una imagen quepa de una vez. En Linux no se toca:
/// fijar SO_SNDBUF apaga el ajuste automático (que llega a 4 MB) y lo recorta
/// a wmem_max (~200 KB).
void tune_tcp(NativeSocket s, bool sending) {
    const int one = 1;
    ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof one);
#ifndef __linux__
    const int big = 4 << 20;
    ::setsockopt(s, SOL_SOCKET, sending ? SO_SNDBUF : SO_RCVBUF, reinterpret_cast<const char*>(&big), sizeof big);
#else
    (void)sending;
#endif
}

bool is_in_progress(int code) {
#ifdef _WIN32
    return code == WSAEWOULDBLOCK || code == WSAEINPROGRESS;
#else
    return code == EINPROGRESS || code == EWOULDBLOCK || code == EAGAIN;
#endif
}

}  // namespace

int wait_io(std::span<const std::intptr_t> sockets, std::size_t n_read, std::chrono::milliseconds timeout,
            std::span<bool> ready) {
    for (auto& r : ready) r = false;
    if (sockets.empty()) {
        if (timeout.count() > 0) std::this_thread::sleep_for(timeout);
        return 0;
    }
#ifdef _WIN32
    std::vector<WSAPOLLFD> fds(sockets.size());
    for (std::size_t i = 0; i < sockets.size(); ++i) {
        fds[i] = WSAPOLLFD{native(sockets[i]), static_cast<SHORT>(i < n_read ? POLLRDNORM : POLLWRNORM), 0};
    }
    const int n = WSAPoll(fds.data(), static_cast<ULONG>(fds.size()), static_cast<INT>(timeout.count()));
#else
    std::vector<pollfd> fds(sockets.size());
    for (std::size_t i = 0; i < sockets.size(); ++i) {
        fds[i] = pollfd{native(sockets[i]), static_cast<short>(i < n_read ? POLLIN : POLLOUT), 0};
    }
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

int wait_rw(std::span<const std::intptr_t> sockets, std::span<const bool> write, std::chrono::milliseconds timeout,
            std::span<bool> readable, std::span<bool> writable) {
    for (auto& r : readable) r = false;
    for (auto& w : writable) w = false;
    if (sockets.empty()) {
        if (timeout.count() > 0) std::this_thread::sleep_for(timeout);
        return 0;
    }
#ifdef _WIN32
    std::vector<WSAPOLLFD> fds(sockets.size());
    for (std::size_t i = 0; i < sockets.size(); ++i) {
        const bool w = i < write.size() && write[i];
        fds[i] = WSAPOLLFD{native(sockets[i]), static_cast<SHORT>(POLLRDNORM | (w ? POLLWRNORM : 0)), 0};
    }
    const int n = WSAPoll(fds.data(), static_cast<ULONG>(fds.size()), static_cast<INT>(timeout.count()));
    constexpr SHORT kRead = POLLRDNORM | POLLHUP | POLLERR | POLLNVAL;
    constexpr SHORT kWrite = POLLWRNORM;
#else
    std::vector<pollfd> fds(sockets.size());
    for (std::size_t i = 0; i < sockets.size(); ++i) {
        const bool w = i < write.size() && write[i];
        fds[i] = pollfd{native(sockets[i]), static_cast<short>(POLLIN | (w ? POLLOUT : 0)), 0};
    }
    const int n = ::poll(fds.data(), static_cast<nfds_t>(fds.size()), static_cast<int>(timeout.count()));
    constexpr short kRead = POLLIN | POLLHUP | POLLERR | POLLNVAL;
    constexpr short kWrite = POLLOUT;
#endif
    if (n < 0) {
        const int code = last_error();
        if (is_would_block(code)) return 0;
        throw NetError(code, "falló la espera de datos");
    }
    int count = 0;
    for (std::size_t i = 0; i < sockets.size(); ++i) {
        const bool r = (fds[i].revents & kRead) != 0;
        const bool w = (fds[i].revents & kWrite) != 0;
        if (i < readable.size()) readable[i] = r;
        if (i < writable.size()) writable[i] = w;
        count += (r || w) ? 1 : 0;
    }
    return count;
}

TcpStream TcpStream::connect(const Address& to, std::chrono::milliseconds timeout) {
    ensure_winsock();
    sockaddr_in addr{};
    std::memcpy(&addr, to.raw_.data(), sizeof addr);
    const NativeSocket s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == kInvalid) throw NetError(last_error(), "no se pudo crear el socket TCP");
    tune_tcp(s, true);
    set_nonblocking(s);
    if (::connect(s, reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0) {
        const int code = last_error();
        if (!is_in_progress(code)) {
            close_native(static_cast<std::intptr_t>(s));
            throw NetError(code, "no se pudo conectar a " + to.to_string());
        }
        // Esperar a que conecte (a 127.0.0.1 tarda microsegundos).
#ifdef _WIN32
        WSAPOLLFD pfd{s, POLLWRNORM, 0};
        const int n = WSAPoll(&pfd, 1, static_cast<INT>(timeout.count()));
#else
        pollfd pfd{s, POLLOUT, 0};
        const int n = ::poll(&pfd, 1, static_cast<int>(timeout.count()));
#endif
        int err = 0;
        socklen_t len = sizeof err;
        if (n <= 0 || ::getsockopt(s, SOL_SOCKET, SO_ERROR, reinterpret_cast<char*>(&err), &len) != 0 || err != 0) {
            close_native(static_cast<std::intptr_t>(s));
            throw NetError(err, "no se pudo conectar a " + to.to_string());
        }
    }
    return TcpStream(static_cast<std::intptr_t>(s), to.to_string());
}

TcpStream TcpStream::connect_async(const Address& to, std::string_view from_ip, bool& pending) {
    ensure_winsock();
    sockaddr_in addr{};
    std::memcpy(&addr, to.raw_.data(), sizeof addr);
    const NativeSocket s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == kInvalid) throw NetError(last_error(), "no se pudo crear el socket TCP");
    TcpStream out(static_cast<std::intptr_t>(s), to.to_string());   // se cierra solo si algo falla
    tune_tcp(s, true);
    if (!from_ip.empty()) {
        const sockaddr_in src = resolve(std::string(from_ip) + ":0");   // AGMEN_NET_IP: sale desde esa IP
        if (::bind(s, reinterpret_cast<const sockaddr*>(&src), sizeof src) != 0) {
            throw NetError(last_error(), "no se pudo salir desde " + std::string(from_ip));
        }
    }
    set_nonblocking(s);
    pending = false;
    if (::connect(s, reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0) {
        const int code = last_error();
        if (!is_in_progress(code)) throw NetError(code, "no se pudo conectar a " + to.to_string());
        pending = true;
    }
    return out;
}

int TcpStream::finish_connect() {
    if (handle_ == -1) return -1;
    int err = 0;
    socklen_t len = sizeof err;
    if (::getsockopt(native(handle_), SOL_SOCKET, SO_ERROR, reinterpret_cast<char*>(&err), &len) != 0) {
        err = last_error();
    }
    if (err != 0) close();
    return err;
}

TcpStream::TcpStream(std::intptr_t handle, std::string peer) : handle_(handle), peer_(std::move(peer)) {
    note_open(handle_);
}

TcpStream::TcpStream(TcpStream&& other) noexcept : handle_(other.handle_), peer_(std::move(other.peer_)) {
    other.handle_ = -1;
}

TcpStream& TcpStream::operator=(TcpStream&& other) noexcept {
    if (this != &other) {
        close();
        handle_ = other.handle_;
        peer_ = std::move(other.peer_);
        other.handle_ = -1;
    }
    return *this;
}

TcpStream::~TcpStream() { close(); }

void TcpStream::close() noexcept {
    if (handle_ == -1) return;
    close_native(handle_);
    handle_ = -1;
}

std::ptrdiff_t TcpStream::send_some(std::span<const std::uint8_t> data) {
    if (handle_ == -1) return -1;
    if (data.empty()) return 0;
#ifdef _WIN32
    const int flags = 0;
#else
    const int flags = MSG_NOSIGNAL;   // un suscriptor que se fue no debe matar al proceso con SIGPIPE
#endif
    const auto n = ::send(native(handle_), reinterpret_cast<const char*>(data.data()),
                          static_cast<LenType>(data.size()), flags);
    if (n < 0) {
        const int code = last_error();
        if (is_would_block(code)) return 0;
        close();
        return -1;
    }
    return static_cast<std::ptrdiff_t>(n);
}

std::ptrdiff_t TcpStream::recv_some(std::span<std::uint8_t> buffer) {
    if (handle_ == -1) return -1;
    if (buffer.empty()) return 0;
    const auto n = ::recv(native(handle_), reinterpret_cast<char*>(buffer.data()),
                          static_cast<LenType>(buffer.size()), 0);
    if (n == 0) {
        close();
        return -1;   // el otro lado cerró
    }
    if (n < 0) {
        const int code = last_error();
        if (is_would_block(code)) return 0;
        close();
        return -1;
    }
    return static_cast<std::ptrdiff_t>(n);
}

TcpListener TcpListener::bind(std::string_view address) {
    ensure_winsock();
    const sockaddr_in addr = resolve(address);
    const NativeSocket s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == kInvalid) throw NetError(last_error(), "no se pudo crear el socket TCP");
#ifndef _WIN32
    const int one = 1;
    ::setsockopt(s, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&one), sizeof one);
#endif
    if (::bind(s, reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0 || ::listen(s, 16) != 0) {
        const int code = last_error();
        close_native(static_cast<std::intptr_t>(s));
        throw NetError(code, "no se pudo escuchar (TCP) en " + std::string(address) +
                                 ". ¿Hay otro proceso usando ese puerto?");
    }
    set_nonblocking(s);
    return TcpListener(static_cast<std::intptr_t>(s));
}

TcpListener::TcpListener(std::intptr_t handle) : handle_(handle) { note_open(handle_); }

TcpListener::TcpListener(TcpListener&& other) noexcept : handle_(other.handle_) { other.handle_ = -1; }

TcpListener& TcpListener::operator=(TcpListener&& other) noexcept {
    if (this != &other) {
        close();
        handle_ = other.handle_;
        other.handle_ = -1;
    }
    return *this;
}

TcpListener::~TcpListener() { close(); }

void TcpListener::close() noexcept {
    if (handle_ == -1) return;
    close_native(handle_);
    handle_ = -1;
}

std::optional<TcpStream> TcpListener::accept() {
    sockaddr_in from{};
    socklen_t len = sizeof from;
    const NativeSocket c = ::accept(native(handle_), reinterpret_cast<sockaddr*>(&from), &len);
    if (c == kInvalid) return std::nullopt;
    tune_tcp(c, false);
    set_nonblocking(c);
    return TcpStream(static_cast<std::intptr_t>(c), to_string(from));
}

std::uint16_t TcpListener::local_port() const {
    sockaddr_in addr{};
    socklen_t len = sizeof addr;
    if (::getsockname(native(handle_), reinterpret_cast<sockaddr*>(&addr), &len) != 0) {
        throw NetError(last_error(), "no se pudo leer el puerto local");
    }
    return ntohs(addr.sin_port);
}

namespace detail {

#ifndef _WIN32
void cover_inherited_sockets() noexcept {
    // Con el intérprete ya en estado normal (no dentro del gancho de pthread_atfork). Se sacan del registro
    // todos (un fork del hijo no los vuelve a tocar) y cada número queda apuntando a /dev/null: el socket
    // se suelta (el puerto ya no es del hijo), pero el número sigue ocupado, así que un objeto heredado
    // que lo cierre después nunca cierra uno nuevo del hijo, y lo que intente mandar o leer falla. Si no se
    // puede (sin /dev/null, o falla dup2), el socket NO se cierra: el hijo lo retiene, y se avisa.
    std::vector<int> fds;
    {
        Registry& r = registry();
        const std::lock_guard lock(r.mu);
        fds.swap(r.fds);
    }
    const int devnull = ::open("/dev/null", O_RDWR | O_CLOEXEC);
    const int open_err = devnull < 0 ? errno : 0;
    int dup_err = 0;
    for (const int fd : fds) {
        if (devnull < 0) break;   // sin /dev/null no se cierra: el número quedaría libre para un socket nuevo
        if (::dup2(devnull, fd) >= 0) {
            (void)::fcntl(fd, F_SETFD, FD_CLOEXEC);
        } else if (dup_err == 0) {
            dup_err = errno;      // este se queda abierto (por la misma razón)
        }
    }
    if (devnull >= 0) (void)::close(devnull);
    if (!fds.empty() && (open_err != 0 || dup_err != 0)) {
        try {   // una vez, y nunca falla en silencio (regla 1)
            log::write(open_err != 0
                           ? "[agmen] ⚠ después del fork no se pudo abrir /dev/null (" + os_message(open_err) +
                                 "): los sockets heredados quedan abiertos en el proceso hijo (retiene sus "
                                 "puertos hasta que termine)"
                           : "[agmen] ⚠ después del fork no se pudo tapar un socket heredado con /dev/null (" +
                                 os_message(dup_err) +
                                 "): queda abierto en el proceso hijo (retiene su puerto hasta que termine)");
        } catch (...) {
            // sin memoria para el aviso: no hay más que hacer
        }
    }
    g_fork_generation.fetch_add(1);
}
#else
void cover_inherited_sockets() noexcept {}
#endif

unsigned fork_generation() noexcept { return g_fork_generation.load(std::memory_order_relaxed); }

}  // namespace detail

}  // namespace agmen::net
