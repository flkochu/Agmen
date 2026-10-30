// agmen/net.hpp — socket UDP multiplataforma (Windows, Linux, macOS)
//
// Esconde las diferencias entre Winsock (Windows) y los sockets BSD
// (Linux/macOS) detrás de una sola clase, para que el resto de Agmen no tenga
// un solo #ifdef.
#pragma once

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
