// agmen/hubproto.hpp — protocolo de control con el `agmen hub` (RFC 0004).
//
// Texto simple: un datagrama UDP = una línea en UTF-8,
//
//   agmen-hub/1 <op> clave=valor clave=valor ...
//
// con "%XX" para los bytes que no sean letras, dígitos o `_ . / : , * -`.
// La especificación completa y la implementación de referencia del hub están
// en python/agmen/hubproto.py y python/agmen/hub.py. Este archivo tiene solo
// lo que un nodo de C++ necesita para registrarse y latir.
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "agmen/net.hpp"
#include "agmen/wire.hpp"

namespace agmen::hub {

inline constexpr std::string_view kPrefix = "agmen-hub/1";
inline constexpr std::string_view kDefaultHub = "127.0.0.1:7390";
inline constexpr std::chrono::milliseconds kBeat{1000};

/// Una línea del protocolo ya leída.
struct Line {
    std::string op;
    std::map<std::string, std::string, std::less<>> fields;

    [[nodiscard]] std::string get(std::string_view key) const {
        const auto it = fields.find(key);
        return it == fields.end() ? std::string() : it->second;
    }
};

[[nodiscard]] std::string quote(std::string_view value);
/// Lanza std::invalid_argument si un "%XX" está mal escrito.
[[nodiscard]] std::string unquote(std::string_view value);

/// "agmen-hub/1 op k=v k=v" (los valores se escapan).
[[nodiscard]] std::string line(std::string_view op,
                               const std::vector<std::pair<std::string, std::string>>& fields);

/// Un datagrama de control → sus líneas. Lanza std::invalid_argument.
[[nodiscard]] std::vector<Line> parse(wire::ByteView data);

/// ¿Es un mensaje de control (y no un mensaje de datos "AGM...")?
[[nodiscard]] bool is_control(wire::ByteView data);

/// "/imu" es un topic; "127.0.0.1:7500" es una dirección.
[[nodiscard]] constexpr bool is_topic(std::string_view target) {
    return !target.empty() && target.front() == '/';
}

/// Nombres válidos: "/imu", "/arca9/articulaciones". Lanza std::invalid_argument
/// con una explicación si no lo es.
void check_topic(std::string_view topic);

/// Dónde está el hub: $AGMEN_HUB o 127.0.0.1:7390.
[[nodiscard]] std::string hub_address();

/// Nombre de este nodo: $AGMEN_NODE (lo pone `agmen run`) o "cpp-<pid>".
[[nodiscard]] std::string node_name();

[[nodiscard]] std::string process_id();

[[nodiscard]] std::string hex16(std::uint64_t v);

/// Registro y latido de UN endpoint (un Publisher o un Subscriber) con el hub.
///
/// Sin hilos: el que usa el socket llama a maybe_beat() seguido (publish()
/// y receive() ya lo hacen) y le pasa a handle() lo que llegue.
class Link {
public:
    /// `beat` = la línea completa de registro ("agmen-hub/1 pub id=... ...").
    Link(std::string id, std::string beat);

    void beat(net::UdpSocket& socket);
    /// Late si pasó ≥ 1 s. Si a los 2 s no contestó ningún hub, avisa una vez.
    void maybe_beat(net::UdpSocket& socket);
    void bye(net::UdpSocket& socket) noexcept;

    /// Si `data` es control, lo procesa y devuelve true. Si trae la lista de
    /// receptores de este endpoint, la deja en `subs`.
    bool handle(wire::ByteView data, std::optional<std::vector<std::string>>& subs);

    [[nodiscard]] bool replied() const { return replied_; }
    [[nodiscard]] const std::string& error() const { return error_; }
    [[nodiscard]] const std::string& id() const { return id_; }

private:
    net::Address hub_;
    std::string id_;
    std::string beat_;
    std::chrono::steady_clock::time_point started_;
    std::chrono::steady_clock::time_point last_beat_{};
    bool replied_ = false;
    bool warned_ = false;
    std::string error_;
};

}  // namespace agmen::hub
