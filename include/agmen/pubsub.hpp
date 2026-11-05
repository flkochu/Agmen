// agmen/pubsub.hpp — mandar y recibir un tipo de mensaje.
//
// Por topic (desde Agmen 0.5, RFC 0004): los nodos se encuentran solos con el
// `agmen hub` de la máquina (lo arranca `agmen run`).
//
//   agmen::Publisher<MotorCommand> pub("/cmd");
//   agmen::Subscriber<MotorCommand> sub("/cmd");     // cuantos quieras
//
// Por dirección fija (como en 0.4; sin hub, para microcontroladores y puentes):
//
//   agmen::Publisher<MotorCommand> pub("127.0.0.1:7500", "/cmd");
//   agmen::Subscriber<MotorCommand> sub("127.0.0.1:7500");
//
//   if (auto s = sub.receive(std::chrono::milliseconds(50))) {
//       use(s->message, s->envelope.seq, s->from);
//   }
//
// Se encargan de: número de secuencia, marcas de tiempo, source_id, anuncios
// de esquema (uno por segundo, y uno directo a cada receptor nuevo) y, en modo
// topic, de registrarse con el hub y latir cada segundo. No usan hilos: el
// latido sale dentro de publish() y receive(). Si un nodo pasa más de 3 s sin
// llamarlos, el hub lo da por muerto; en esos ciclos lentos, llama heartbeat().
//
// Los mensajes compact viajan en un datagrama UDP. Los de tipos con campos de
// tamaño variable (string, bytes, T[], tensor; codificación arrow, RFC 0008)
// pueden medir megabytes y van por TCP: el Subscriber escucha en un puerto TCP
// (que registra en el hub) y el Publisher se conecta a cada uno. Un hilo del
// Publisher termina de mandar lo que no cupo de una vez; a un suscriptor lento
// se le guarda solo el mensaje más nuevo (dropped() cuenta los que no se
// entregaron). Un suscriptor lento nunca frena al publicador ni a los demás.
//
// Memoria compartida (desde Agmen 0.11, RFC 0009): si el suscriptor está en la
// misma computadora y puede abrir la memoria del publicador (lo prueban al
// conectarse), un mensaje arrow de 4 KiB o más se escribe UNA vez en un bloque
// de memoria compartida y por TCP viaja solo un descriptor. El suscriptor
// recibe una vista del bloque (RawSample::payload, sin copiar) y se lo devuelve
// al publicador cuando el último Payload que lo ve se destruye. Si no puede
// (otra computadora, otro contenedor, AGMEN_SHM=0), todo sigue por TCP.
//
// Por dentro, Publisher<M> y Subscriber<M> son una capa delgada sobre
// PublisherCore y SubscriberCore, que no dependen del tipo: los usa también el
// módulo de Python (agmen._core) con tipos que no se compilaron (RFC 0007).
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "agmen/hubproto.hpp"
#include "agmen/net.hpp"
#include "agmen/wire.hpp"

namespace agmen {

/// Cada cuánto el Publisher repite el anuncio de su esquema.
inline constexpr std::chrono::milliseconds kAnnounceEvery{1000};

/// Lo que define a un tipo de mensaje sin conocerlo en compilación.
struct TypeInfo {
    std::string name;          ///< "MotorCommand"
    std::string schema_text;   ///< canónico completo: "agmen/1 MotorCommand{...}"
    std::uint64_t schema_id = 0;
    std::size_t payload_size = 0;   ///< tamaño compact (0 = no revisar)
    bool arrow = false;             ///< campos de tamaño variable: codificación arrow, por TCP (RFC 0008)

    template <wire::Message M>
    [[nodiscard]] static TypeInfo of() {
        return TypeInfo{std::string(M::kName), wire::schema_text<M>(), wire::schema_id_v<M>,
                        wire::compact_size_v<M>, wire::is_arrow_v<M>};
    }
};

/// Publica payloads de un tipo (ya codificados). Seguro entre hilos.
class PublisherCore {
public:
    /// `to` = topic ("/cmd") o dirección ("127.0.0.1:7500"); con dirección,
    /// `topic` es la etiqueta que viaja en el sobre ("" = "/" + nombre del tipo).
    PublisherCore(const std::string& to, const TypeInfo& type, std::string topic = "");
    PublisherCore(PublisherCore&&) noexcept;
    PublisherCore& operator=(PublisherCore&&) noexcept;
    PublisherCore(const PublisherCore&) = delete;
    PublisherCore& operator=(const PublisherCore&) = delete;
    ~PublisherCore();

    /// Manda un payload (compact o arrow, según el tipo); t_data en ns de Agmen.
    net::SendStatus publish_payload(wire::ByteView payload, std::int64_t t_data);
    void announce();
    void heartbeat();

    [[nodiscard]] std::uint64_t seq() const;
    [[nodiscard]] std::uint64_t source_id() const;
    [[nodiscard]] const std::string& topic() const;
    [[nodiscard]] std::size_t subscribers() const;
    [[nodiscard]] const TypeInfo& type() const;
    /// Mensajes arrow que no se entregaron a algún suscriptor lento (RFC 0008 D3).
    [[nodiscard]] std::uint64_t dropped() const;
    /// A cuántos suscriptores se les manda por memoria compartida (RFC 0009).
    [[nodiscard]] std::size_t shm_subscribers() const;
    /// Mensajes que debían ir por memoria compartida y fueron por TCP porque no
    /// hubo lugar (se avisa una vez por qué; RFC 0009 D6).
    [[nodiscard]] std::uint64_t shm_fallbacks() const;
    /// Bytes de memoria compartida que este publicador tiene apartados ahora
    /// (sus bloques de datos). Baja solo: un bloque sin usar 5 s se libera (D6 a′).
    [[nodiscard]] std::size_t shm_bytes() const;

private:
    struct State;
    std::unique_ptr<State> s_;
};

/// El payload de un mensaje recibido. Casi siempre son bytes propios; si llegó
/// por memoria compartida (RFC 0009), es una vista del bloque del publicador,
/// que se le devuelve cuando el último Payload que lo ve se destruye. Se usa
/// donde va un wire::ByteView (std::span lo acepta solo: data(), size(), begin(), end()).
class Payload {
public:
    Payload() = default;
    Payload(wire::Bytes bytes) : own_(std::move(bytes)) {}   // NOLINT: implícito a propósito
    /// Una vista de memoria que vive mientras viva `keep`.
    Payload(wire::ByteView view, std::shared_ptr<const void> keep) : view_(view), keep_(std::move(keep)) {}

    [[nodiscard]] const std::uint8_t* data() const { return keep_ ? view_.data() : own_.data(); }
    [[nodiscard]] std::size_t size() const { return keep_ ? view_.size() : own_.size(); }
    [[nodiscard]] bool empty() const { return size() == 0; }
    [[nodiscard]] const std::uint8_t* begin() const { return data(); }
    [[nodiscard]] const std::uint8_t* end() const { return data() + size(); }
    [[nodiscard]] wire::ByteView view() const { return {data(), size()}; }
    /// ¿Es una vista de la memoria compartida del publicador?
    [[nodiscard]] bool shared() const { return static_cast<bool>(keep_); }
    /// Una copia de los bytes.
    [[nodiscard]] wire::Bytes to_bytes() const { return wire::Bytes(begin(), end()); }

private:
    wire::Bytes own_;
    wire::ByteView view_{};
    std::shared_ptr<const void> keep_;
};

/// Un mensaje recibido, todavía sin decodificar el payload.
struct RawSample {
    wire::Envelope envelope;   ///< con kFlagShm si llegó por memoria compartida; payload_len = bytes de datos
    Payload payload;
    std::string from;   ///< "ip:puerto"
};

/// Recibe mensajes de un tipo, sin decodificar el payload. Valida el sobre, el
/// tipo y el tamaño; procesa los anuncios y el control del hub. heartbeat() se
/// puede llamar desde otro hilo mientras uno espera en receive_raw().
class SubscriberCore {
public:
    SubscriberCore(const std::string& where, const TypeInfo& type);
    SubscriberCore(SubscriberCore&&) noexcept;
    SubscriberCore& operator=(SubscriberCore&&) noexcept;
    SubscriberCore(const SubscriberCore&) = delete;
    SubscriberCore& operator=(const SubscriberCore&) = delete;
    ~SubscriberCore();

    /// Espera hasta `timeout` un mensaje. std::nullopt si no llegó. Lanza
    /// wire::WireError si llega algo inválido o de otro tipo.
    [[nodiscard]] std::optional<RawSample> receive_raw(std::chrono::milliseconds timeout);
    void heartbeat();
    /// Suelta los bloques de memoria compartida que tenía abiertos para
    /// recibir y que ningún mensaje usa desde hace 5 s (D6 a′; los prestados
    /// siguen hasta que los sueltes). receive_raw() lo hace solo, una vez por
    /// segundo; el Node, en cada latido. Llámalo desde el hilo que recibe.
    void forget_idle_blocks();
    /// Bloques de memoria compartida que tiene abiertos para recibir.
    [[nodiscard]] std::size_t shm_mapped() const;

    [[nodiscard]] const std::string& last_from() const;
    /// El socket UDP (para esperar varios a la vez). Un tipo arrow también
    /// tiene los de TCP: native_handles() los da todos.
    [[nodiscard]] std::intptr_t native_handle() const;
    [[nodiscard]] std::vector<std::intptr_t> native_handles() const;
    /// Puerto local (UDP) donde escucha (útil con "127.0.0.1:0").
    [[nodiscard]] std::uint16_t local_port() const;
    /// Puerto TCP donde recibe mensajes arrow (0 si el tipo no los usa).
    [[nodiscard]] std::uint16_t tcp_port() const;
    [[nodiscard]] const std::string& where() const;
    [[nodiscard]] const TypeInfo& type() const;
    /// Anuncios vistos: schema_id → (topic, esquema).
    [[nodiscard]] const std::map<std::uint64_t, wire::Announcement>& announced() const;

private:
    struct State;
    std::unique_ptr<State> s_;
};

template <wire::Message M>
class Publisher {
public:
    /// `to` = topic ("/cmd") o dirección del receptor ("127.0.0.1:7500"). Con
    /// dirección, `topic` es solo una etiqueta que viaja en el sobre.
    explicit Publisher(const std::string& to, std::string topic = "/" + std::string(M::kName))
        : core_(to, TypeInfo::of<M>(), std::move(topic)) {
        AGMEN_CHECK_SCHEMA(M);
    }

    /// Manda `msg` con t_data = ahora. NoListener = no hay a quién entregarlo
    /// (no es un error). Lanza WireError si el hub rechazó el topic.
    net::SendStatus publish(const M& msg) { return publish(msg, wire::now_ns()); }

    /// Manda `msg`; t_data = instante al que corresponden los datos (ej. la
    /// captura del sensor), en ns de Agmen.
    net::SendStatus publish(const M& msg, std::int64_t t_data) {
        wire::Bytes payload;
        payload.reserve(wire::compact_size_v<M>);
        msg.encode_payload(payload);
        if constexpr (!wire::is_arrow_v<M>) {
            if (payload.size() != wire::compact_size_v<M>) {
                // Un error de programación: el encode_payload no sigue al esquema.
                throw std::logic_error("encode_payload de '" + std::string(M::kName) + "' produjo " +
                                       std::to_string(payload.size()) + " bytes, pero su esquema mide " +
                                       std::to_string(wire::compact_size_v<M>));
            }
        }
        return core_.publish_payload(payload, t_data);
    }

    /// Manda el anuncio del esquema ya, a todos los receptores (publish() lo hace solo).
    void announce() { core_.announce(); }

    /// Late con el hub y lee sus respuestas. Llámalo si tu ciclo publica menos
    /// de una vez cada 3 s (si no, publish() ya lo hace).
    void heartbeat() { core_.heartbeat(); }

    [[nodiscard]] std::uint64_t seq() const { return core_.seq(); }
    [[nodiscard]] std::uint64_t source_id() const { return core_.source_id(); }
    [[nodiscard]] const std::string& topic() const { return core_.topic(); }
    /// A cuántos receptores se manda cada mensaje.
    [[nodiscard]] std::size_t subscribers() const { return core_.subscribers(); }
    /// Mensajes que no se entregaron a algún suscriptor lento (solo tipos arrow, RFC 0008 D3).
    [[nodiscard]] std::uint64_t dropped() const { return core_.dropped(); }
    /// A cuántos suscriptores se les manda por memoria compartida (tipos arrow, RFC 0009).
    [[nodiscard]] std::size_t shm_subscribers() const { return core_.shm_subscribers(); }
    /// Mensajes que no cupieron en la memoria compartida y fueron por TCP (RFC 0009 D6).
    [[nodiscard]] std::uint64_t shm_fallbacks() const { return core_.shm_fallbacks(); }
    /// Bytes de memoria compartida apartados ahora (RFC 0009 D6 a′).
    [[nodiscard]] std::size_t shm_bytes() const { return core_.shm_bytes(); }

private:
    PublisherCore core_;
};

/// Un mensaje recibido.
template <wire::Message M>
struct Sample {
    wire::Envelope envelope;
    M message;
    std::string from;  ///< "ip:puerto" de quien lo mandó
};

template <wire::Message M>
class Subscriber {
public:
    /// `where` = topic ("/cmd") o dirección donde escuchar ("127.0.0.1:7500").
    explicit Subscriber(const std::string& where) : core_(where, TypeInfo::of<M>()) { AGMEN_CHECK_SCHEMA(M); }

    /// Espera hasta `timeout` un mensaje de tipo M. std::nullopt si no llegó.
    /// Lanza wire::WireError si llega algo inválido, de otro tipo, o si el hub
    /// rechazó el topic.
    [[nodiscard]] std::optional<Sample<M>> receive(std::chrono::milliseconds timeout) {
        auto raw = core_.receive_raw(timeout);
        if (!raw) return std::nullopt;
        return Sample<M>{raw->envelope, M::decode_payload(raw->payload), std::move(raw->from)};
    }

    /// Como receive(), sin decodificar: el payload llega como lo mandaron. Si
    /// vino por memoria compartida, es una vista del bloque sin copiar (decodifícalo
    /// con agmen::arrow::Codec mientras tengas el RawSample; RFC 0009).
    [[nodiscard]] std::optional<RawSample> receive_raw(std::chrono::milliseconds timeout) {
        return core_.receive_raw(timeout);
    }

    /// Late con el hub. Llámalo si pasas más de 3 s sin llamar receive().
    void heartbeat() { core_.heartbeat(); }
    /// Suelta los bloques de memoria compartida que ningún mensaje usa desde
    /// hace 5 s (receive() lo hace solo, una vez por segundo; RFC 0009 D6 a′).
    void forget_idle_blocks() { core_.forget_idle_blocks(); }
    /// Bloques de memoria compartida que tiene abiertos para recibir (RFC 0009).
    [[nodiscard]] std::size_t shm_mapped() const { return core_.shm_mapped(); }

    /// "ip:puerto" del último paquete recibido (útil al reportar un error).
    [[nodiscard]] const std::string& last_from() const { return core_.last_from(); }

    /// El socket, para que un Node espere varias suscripciones a la vez.
    [[nodiscard]] std::intptr_t native_handle() const { return core_.native_handle(); }
    /// Todos los sockets (UDP, y los de TCP si el tipo es arrow).
    [[nodiscard]] std::vector<std::intptr_t> native_handles() const { return core_.native_handles(); }
    /// Puerto local (UDP) donde escucha (útil con "127.0.0.1:0").
    [[nodiscard]] std::uint16_t local_port() const { return core_.local_port(); }
    /// Puerto TCP donde recibe mensajes arrow (0 si el tipo no los usa).
    [[nodiscard]] std::uint16_t tcp_port() const { return core_.tcp_port(); }

    /// Topic o dirección donde escucha.
    [[nodiscard]] const std::string& where() const { return core_.where(); }

    /// Anuncios vistos: schema_id → (topic, esquema).
    [[nodiscard]] const std::map<std::uint64_t, wire::Announcement>& announced() const {
        return core_.announced();
    }

private:
    SubscriberCore core_;
};

}  // namespace agmen
