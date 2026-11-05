// agmen/net.hpp — socket UDP multiplataforma (Windows, Linux, macOS)
//
// Esconde las diferencias entre Winsock (Windows) y los sockets BSD
// (Linux/macOS) detrás de una sola clase, para que el resto de Agmen no tenga
// un solo #ifdef.
#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <system_error>

namespace agmen::net {

/// Error de red con un mensaje claro y el código del sistema operativo.
class NetError : public std::system_error {
public:
    NetError(int os_code, const std::string& what);
};

/// Resultado de un envío.
enum class SendStatus {
    Ok,
    /// Nadie escucha del otro lado. Linux/macOS lo reportan como
    /// ECONNREFUSED y Windows como WSAECONNRESET. No es fatal.
    NoListener,
};

/// Dirección IPv4 ya resuelta ("ip:puerto"), para mandar muchas veces sin
/// resolverla de nuevo.
class Address {
public:
    /// Acepta nombres ("localhost:7500"). Lanza NetError si no se puede resolver.
    [[nodiscard]] static Address resolve(std::string_view address);

    [[nodiscard]] std::string to_string() const;
    friend bool operator==(const Address&, const Address&) = default;

private:
    friend class UdpSocket;
    friend class TcpStream;
    std::array<std::uint8_t, 16> raw_{};  // un sockaddr_in
};

/// Datagrama recibido.
struct Received {
    std::size_t size = 0;
    std::string from;  ///< "ip:puerto" del emisor
};

class UdpSocket {
public:
    /// Abre un socket escuchando en "ip:puerto" (ej. "127.0.0.1:7400",
    /// "0.0.0.0:7400" para todas las interfaces, "0.0.0.0:0" = puerto libre).
    [[nodiscard]] static UdpSocket bind(std::string_view address);

    UdpSocket(UdpSocket&& other) noexcept;
    UdpSocket& operator=(UdpSocket&& other) noexcept;
    UdpSocket(const UdpSocket&) = delete;
    UdpSocket& operator=(const UdpSocket&) = delete;
    ~UdpSocket();

    /// Fija el destino por defecto para send(). Acepta nombres ("localhost").
    void connect(std::string_view address);

    /// Envía un datagrama al destino fijado con connect().
    SendStatus send(std::span<const std::uint8_t> data);

    /// Envía un datagrama a `to` (sin necesidad de connect()).
    SendStatus send_to(std::span<const std::uint8_t> data, const Address& to);

    /// false si el socket fue movido a otro objeto.
    [[nodiscard]] bool valid() const { return handle_ != -1; }

    /// El socket del sistema operativo (para esperar varios a la vez con wait_readable).
    [[nodiscard]] std::intptr_t native_handle() const { return handle_; }

    /// Puerto local en el que quedó el socket (útil si se abrió con puerto 0).
    [[nodiscard]] std::uint16_t local_port() const;

    /// Espera un datagrama hasta `timeout`. Devuelve std::nullopt si no llegó
    /// nada a tiempo.
    [[nodiscard]] std::optional<Received> recv_from(std::span<std::uint8_t> buffer,
                                                    std::chrono::milliseconds timeout);

private:
    explicit UdpSocket(std::intptr_t handle) : handle_(handle) {}
    void close() noexcept;

    std::intptr_t handle_ = -1;  // SOCKET en Windows, int en POSIX
};

/// Espera hasta `timeout` a que alguno de los sockets tenga datos. Marca en
/// `ready` cuáles los tienen y devuelve cuántos (0 = nadie a tiempo).
int wait_readable(std::span<const std::intptr_t> sockets, std::chrono::milliseconds timeout, std::span<bool> ready);

/// Como wait_readable, pero espera lectura en los primeros `n_read` sockets y
/// escritura en el resto. Devuelve cuántos están listos.
int wait_io(std::span<const std::intptr_t> sockets, std::size_t n_read, std::chrono::milliseconds timeout,
            std::span<bool> ready);

/// Espera lectura en todos los sockets y, además, escritura en los que tengan
/// `write[i]`. Deja en `readable` / `writable` qué pasó con cada uno (un
/// socket cerrado o con error cuenta como legible: recv dirá -1). Devuelve
/// cuántos están listos (0 = nadie a tiempo).
int wait_rw(std::span<const std::intptr_t> sockets, std::span<const bool> write, std::chrono::milliseconds timeout,
            std::span<bool> readable, std::span<bool> writable);

// ---------------------------------------------------------------------------
// TCP: el transporte de los mensajes grandes (arrow, RFC 0008). Los sockets
// son no bloqueantes: quien los usa espera con wait_readable / wait_io.
// ---------------------------------------------------------------------------

/// Una conexión TCP (del lado que se conecta o del que acepta).
class TcpStream {
public:
    /// Se conecta a "ip:puerto" esperando hasta `timeout`. Lanza NetError si no se pudo.
    [[nodiscard]] static TcpStream connect(const Address& to, std::chrono::milliseconds timeout);

    TcpStream() = default;
    TcpStream(TcpStream&& other) noexcept;
    TcpStream& operator=(TcpStream&& other) noexcept;
    TcpStream(const TcpStream&) = delete;
    TcpStream& operator=(const TcpStream&) = delete;
    ~TcpStream();

    /// Manda lo que el sistema acepte ahora: cuántos bytes salieron (0 si no
    /// acepta nada por el momento). -1 si la conexión se cerró o falló.
    std::ptrdiff_t send_some(std::span<const std::uint8_t> data);
    /// Lee lo que haya: cuántos bytes llegaron (0 si no hay nada por ahora).
    /// -1 si el otro lado cerró o falló.
    std::ptrdiff_t recv_some(std::span<std::uint8_t> buffer);

    [[nodiscard]] bool valid() const { return handle_ != -1; }
    [[nodiscard]] std::intptr_t native_handle() const { return handle_; }
    [[nodiscard]] const std::string& peer() const { return peer_; }
    void close() noexcept;

private:
    friend class TcpListener;
    TcpStream(std::intptr_t handle, std::string peer) : handle_(handle), peer_(std::move(peer)) {}

    std::intptr_t handle_ = -1;
    std::string peer_;   ///< "ip:puerto" del otro lado
};

/// Un puerto TCP que acepta conexiones (no bloqueante).
class TcpListener {
public:
    /// Escucha en "ip:puerto" ("127.0.0.1:0" = puerto libre). Lanza NetError.
    [[nodiscard]] static TcpListener bind(std::string_view address);

    TcpListener() = default;
    TcpListener(TcpListener&& other) noexcept;
    TcpListener& operator=(TcpListener&& other) noexcept;
    TcpListener(const TcpListener&) = delete;
    TcpListener& operator=(const TcpListener&) = delete;
    ~TcpListener();

    /// La siguiente conexión pendiente, si hay (no espera).
    [[nodiscard]] std::optional<TcpStream> accept();

    [[nodiscard]] bool valid() const { return handle_ != -1; }
    [[nodiscard]] std::intptr_t native_handle() const { return handle_; }
    [[nodiscard]] std::uint16_t local_port() const;
    void close() noexcept;

private:
    explicit TcpListener(std::intptr_t handle) : handle_(handle) {}
    std::intptr_t handle_ = -1;
};

}  // namespace agmen::net
