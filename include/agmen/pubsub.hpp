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

    template <wire::Message M>
    [[nodiscard]] static TypeInfo of() {
        return TypeInfo{std::string(M::kName), wire::schema_text<M>(), wire::schema_id_v<M>,
                        wire::compact_size_v<M>};
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

    /// Manda un payload compact; t_data en ns de Agmen.
    net::SendStatus publish_payload(wire::ByteView payload, std::int64_t t_data);
    void announce();
    void heartbeat();

    [[nodiscard]] std::uint64_t seq() const;
    [[nodiscard]] std::uint64_t source_id() const;
    [[nodiscard]] const std::string& topic() const;
    [[nodiscard]] std::size_t subscribers() const;
    [[nodiscard]] const TypeInfo& type() const;

private:
    struct State;
    std::unique_ptr<State> s_;
};

/// Un mensaje recibido, todavía sin decodificar el payload.
struct RawSample {
    wire::Envelope envelope;
    wire::Bytes payload;
    std::string from;   ///< "ip:puerto"
};

/// Recibe mensajes de un tipo, sin decodificar el payload. Valida el sobre, el
/// tipo y el tamaño; procesa los anuncios y el control del hub.
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

    [[nodiscard]] const std::string& last_from() const;
    [[nodiscard]] std::intptr_t native_handle() const;
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
        if (payload.size() != wire::compact_size_v<M>) {
            // Un error de programación: el encode_payload no sigue al esquema.
            throw std::logic_error("encode_payload de '" + std::string(M::kName) + "' produjo " +
                                   std::to_string(payload.size()) + " bytes, pero su esquema mide " +
                                   std::to_string(wire::compact_size_v<M>));
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

    /// Late con el hub. Llámalo si pasas más de 3 s sin llamar receive().
    void heartbeat() { core_.heartbeat(); }

    /// "ip:puerto" del último paquete recibido (útil al reportar un error).
    [[nodiscard]] const std::string& last_from() const { return core_.last_from(); }

    /// El socket, para que un Node espere varias suscripciones a la vez.
    [[nodiscard]] std::intptr_t native_handle() const { return core_.native_handle(); }

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
