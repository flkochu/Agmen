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

}  // namespace agmen::net
