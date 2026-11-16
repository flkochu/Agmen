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
#include <set>
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

// --- Modo red (RFC 0011 §3.5) -------------------------------------------------
// Los mismos casos que python/agmen/hubproto.py (python/test_hub.py, TestModoRedProtocolo).
//
//   red    peers=nombre@ip,...   (de esta computadora al hub; vacío = volver a modo local)
//   red_ok peers=n               (el hub: cuántas computadoras aceptó)
//   pong   version [red=1]       ok id [peers=ip,ip]       sub ... [net=1]
//   sub ... machine addr=ip:puerto [tcp]   pub ... machine   (entradas de otra computadora)

inline constexpr std::string_view kLocalhost = "127.0.0.1";
inline constexpr std::chrono::milliseconds kNetBeat{2000};       // quien encendió el modo red lo repite
inline constexpr std::chrono::milliseconds kNetExpire{6000};     // sin otro `red`: vuelta a modo local
inline constexpr std::chrono::milliseconds kNetLocalWarn{5000};  // sub local sin net=1: aviso

/// "a,b,,c" → {"a", "b", "c"}.
[[nodiscard]] std::vector<std::string> split_list(std::string_view list);

/// Nombre de [[machine]]: letras, dígitos, '_' o '-'. Lanza std::invalid_argument.
void check_machine(std::string_view name);

/// IPv4 escrita como cuatro números de 0 a 255 (sin ceros a la izquierda). La devuelve
/// tal cual; lanza std::invalid_argument si no lo es.
[[nodiscard]] std::string parse_ipv4(std::string_view text);

/// La IP de OTRA computadora: válida, que no sea 127.0.0.1 ni una dirección especial o de grupo.
/// `loopback`: acepta las demás 127.x (solo pruebas con AGMEN_NET_IP, RFC 0011 §9.6).
void check_peer_ip(std::string_view ip, bool loopback = false);

/// Puerto de 1 a 65535. `what` nombra el campo en el error.
[[nodiscard]] std::uint16_t parse_port(std::string_view text, std::string_view what = "puerto");

/// "192.168.1.20:7500" → {"192.168.1.20", 7500}. Lanza std::invalid_argument.
[[nodiscard]] std::pair<std::string, std::uint16_t> parse_hostport(std::string_view text);

/// "robot@192.168.1.20,laptop@10.0.0.5" → {{"robot", "192.168.1.20"}, ...}; "" = ninguna.
/// Nombres repetidos (sin importar mayúsculas) o IP repetidas: std::invalid_argument.
/// `loopback`: acepta IP 127.x (pruebas con AGMEN_NET_IP; ver check_peer_ip).
[[nodiscard]] std::vector<std::pair<std::string, std::string>> parse_peers(std::string_view text,
                                                                          bool loopback = false);

[[nodiscard]] std::string format_peers(const std::vector<std::pair<std::string, std::string>>& peers);

/// Dónde está el hub: $AGMEN_HUB o 127.0.0.1:7390.
[[nodiscard]] std::string hub_address();

/// Nombre de este nodo: $AGMEN_NODE (lo pone `agmen run`), el de agmen::Node, o "cpp-<pid>".
[[nodiscard]] std::string node_name();

/// El nombre a usar si no hay $AGMEN_NODE (lo llama agmen::Node).
void set_default_node_name(std::string name);

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
    /// receptores de este endpoint, la deja en `subs` (los puertos UDP) y, si
    /// se pide, en `tcp` (los puertos TCP de los que reciben arrow, RFC 0008).
    bool handle(wire::ByteView data, std::optional<std::vector<std::string>>& subs,
                std::optional<std::vector<std::string>>* tcp = nullptr);

    [[nodiscard]] bool replied() const { return replied_; }
    [[nodiscard]] const std::string& error() const { return error_; }
    [[nodiscard]] const std::string& id() const { return id_; }
    /// Las IP del campo `peers` del último `ok` (modo red, RFC 0011 §3.5); sin valor si el
    /// último `ok` no lo traía (modo local).
    [[nodiscard]] const std::optional<std::vector<std::string>>& peers() const { return peers_; }
    /// Cuántos `ok` de este endpoint llegaron (el Subscriber que pasa a la red espera el primero
    /// que llega por su socket nuevo, RFC 0011 §2).
    [[nodiscard]] std::uint64_t oks() const { return oks_; }
    /// Cambia la línea del latido (el Subscriber que pasa a la red: puerto nuevo y net=1).
    void set_beat(std::string beat) { beat_ = std::move(beat); }
    /// ¿`from` ("ip:puerto", el remitente de un datagrama) es el hub? El control de cualquier otro se
    /// descarta sin efecto (RFC 0011 §9.1).
    [[nodiscard]] bool from_hub(std::string_view from) const { return from == hub_from_; }
    /// Llegó control de `from`, que no es el hub: se avisa una vez por remitente (con tope).
    void warn_foreign(std::string_view topic, const std::string& from);

private:
    net::Address hub_;
    std::string hub_from_;   // "ip:puerto" del que llegan sus respuestas (hub_ resuelto)
    std::set<std::string> foreign_warned_;
    std::string id_;
    std::string beat_;
    std::chrono::steady_clock::time_point started_;
    std::chrono::steady_clock::time_point last_beat_{};
    bool replied_ = false;
    bool warned_ = false;
    std::string error_;
    std::optional<std::vector<std::string>> peers_;
    std::uint64_t oks_ = 0;
};

}  // namespace agmen::hub
