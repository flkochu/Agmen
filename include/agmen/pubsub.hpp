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
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
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

namespace detail {

inline void check_topic_or_throw(const std::string& topic) {
    try {
        hub::check_topic(topic);
    } catch (const std::invalid_argument& e) {
        throw wire::WireError(wire::WireError::Kind::HubRejected, e.what());
    }
}

}  // namespace detail

template <wire::Message M>
class Publisher {
public:
    /// `to` = topic ("/cmd") o dirección del receptor ("127.0.0.1:7500"). Con
    /// dirección, `topic` es solo una etiqueta que viaja en el sobre.
    explicit Publisher(const std::string& to, std::string topic = "/" + std::string(M::kName))
        // Por topic todo es local (nivel 2): 127.0.0.1, así Windows no pregunta por el firewall.
        : socket_(net::UdpSocket::bind(hub::is_topic(to) ? "127.0.0.1:0" : "0.0.0.0:0")),
          topic_(hub::is_topic(to) ? to : std::move(topic)),
          source_id_(wire::random_source_id()),
          schema_text_(wire::schema_text<M>()),
          buf_(65536) {
        AGMEN_CHECK_SCHEMA(M);
        if (hub::is_topic(to)) {
            detail::check_topic_or_throw(to);
            const auto id = hub::hex16(source_id_);
            link_.emplace(id, hub::line("pub", {{"id", id},
                                                {"node", hub::node_name()},
                                                {"pid", hub::process_id()},
                                                {"topic", topic_},
                                                {"schema", hub::hex16(wire::schema_id_v<M>)},
                                                {"type", std::string(M::kName)},
                                                {"text", schema_text_}}));
            link_->beat(socket_);
            // Espera breve a la primera respuesta del hub: si ya hay receptores,
            // el primer mensaje no se pierde por no saber a quién mandarlo.
            const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(100);
            while (!link_->replied() && std::chrono::steady_clock::now() < end) {
                drain(std::chrono::milliseconds(10));
            }
        } else {
            dests_.push_back(net::Address::resolve(to));
        }
        topic_id_ = wire::topic_id(topic_);
    }

    Publisher(Publisher&&) noexcept = default;
    Publisher& operator=(Publisher&&) noexcept = default;
    Publisher(const Publisher&) = delete;
    Publisher& operator=(const Publisher&) = delete;
    ~Publisher() {
        if (link_ && socket_.valid()) link_->bye(socket_);
    }

    /// Manda `msg` con t_data = ahora. NoListener = no hay a quién entregarlo
    /// (no es un error). Lanza WireError si el hub rechazó el topic.
    net::SendStatus publish(const M& msg) { return publish(msg, wire::now_ns()); }

    /// Manda `msg`; t_data = instante al que corresponden los datos (ej. la
    /// captura del sensor), en ns desde la época Unix.
    net::SendStatus publish(const M& msg, std::int64_t t_data) {
        if (link_) {
            link_->maybe_beat(socket_);
            drain(std::chrono::milliseconds(0));
            if (!link_->error().empty()) {
                throw wire::WireError(wire::WireError::Kind::HubRejected,
                                      "el hub rechazó publicar en '" + topic_ + "': " +
                                          link_->error());
            }
        }
        const auto now = std::chrono::steady_clock::now();
        if (!announced_ || now - last_announce_ >= kAnnounceEvery) announce();
        const auto packet = wire::encode(msg, wire::Envelope{
                                                  .topic_id = topic_id_,
                                                  .source_id = source_id_,
                                                  .seq = seq_,
                                                  .t_data = t_data,
                                                  .t_pub = wire::now_ns(),
                                              });
        ++seq_;
        auto status = net::SendStatus::NoListener;
        for (const auto& d : dests_) {
            if (socket_.send_to(packet, d) == net::SendStatus::Ok) status = net::SendStatus::Ok;
        }
        return status;
    }

    /// Manda el anuncio del esquema ya, a todos los receptores (publish() lo hace solo).
    void announce() {
        const auto ann = announcement();
        for (const auto& d : dests_) (void)socket_.send_to(ann, d);
        announced_ = true;
        last_announce_ = std::chrono::steady_clock::now();
    }

    /// Late con el hub y lee sus respuestas. Llámalo si tu ciclo publica menos
    /// de una vez cada 3 s (si no, publish() ya lo hace).
    void heartbeat() {
        if (!link_) return;
        link_->maybe_beat(socket_);
        drain(std::chrono::milliseconds(0));
    }

    [[nodiscard]] std::uint64_t seq() const { return seq_; }
    [[nodiscard]] std::uint64_t source_id() const { return source_id_; }
    [[nodiscard]] const std::string& topic() const { return topic_; }
    /// A cuántos receptores se manda cada mensaje.
    [[nodiscard]] std::size_t subscribers() const { return dests_.size(); }

private:
    wire::Bytes announcement() const {
        return wire::encode_announcement(
            schema_text_, topic_,
            wire::Envelope{.source_id = source_id_, .seq = seq_, .t_pub = wire::now_ns()});
    }

    /// Lee las respuestas del hub (a quién mandarle los datos).
    void drain(std::chrono::milliseconds wait) {
        while (const auto rx = socket_.recv_from(buf_, wait)) {
            std::optional<std::vector<std::string>> subs;
            link_->handle(wire::ByteView(buf_.data(), rx->size), subs);
            if (subs) {
                std::vector<net::Address> fresh;
                fresh.reserve(subs->size());
                const auto ann = announcement();
                for (const auto& a : *subs) {
                    fresh.push_back(net::Address::resolve(a));
                    bool known = false;
                    for (const auto& d : dests_) known = known || d == fresh.back();
                    if (!known) (void)socket_.send_to(ann, fresh.back());  // recién llegado
                }
                dests_ = std::move(fresh);
            }
            wait = std::chrono::milliseconds(0);
        }
    }

    net::UdpSocket socket_;
    std::string topic_;
    std::uint64_t topic_id_ = 0;
    std::uint64_t source_id_;
    std::string schema_text_;
    std::vector<std::uint8_t> buf_;
    std::vector<net::Address> dests_;
    std::optional<hub::Link> link_;
    std::uint64_t seq_ = 0;
    bool announced_ = false;
    std::chrono::steady_clock::time_point last_announce_{};
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
    explicit Subscriber(const std::string& where)
        : socket_(net::UdpSocket::bind(hub::is_topic(where) ? "127.0.0.1:0" : where)),
          buf_(65536) {
        AGMEN_CHECK_SCHEMA(M);
        if (hub::is_topic(where)) {
            detail::check_topic_or_throw(where);
            topic_ = where;
            const auto id = hub::hex16(wire::random_source_id());
            link_.emplace(id, hub::line("sub", {{"id", id},
                                                {"node", hub::node_name()},
                                                {"pid", hub::process_id()},
                                                {"topic", where},
                                                {"schema", hub::hex16(wire::schema_id_v<M>)},
                                                {"type", std::string(M::kName)},
                                                {"port", std::to_string(socket_.local_port())}}));
            link_->beat(socket_);
        }
    }

    Subscriber(Subscriber&&) noexcept = default;
    Subscriber& operator=(Subscriber&&) noexcept = default;
    Subscriber(const Subscriber&) = delete;
    Subscriber& operator=(const Subscriber&) = delete;
    ~Subscriber() {
        if (link_ && socket_.valid()) link_->bye(socket_);
    }

    /// Espera hasta `timeout` un mensaje de tipo M. std::nullopt si no llegó.
    /// Lanza wire::WireError si llega algo inválido, de otro tipo, o si el hub
    /// rechazó el topic.
    [[nodiscard]] std::optional<Sample<M>> receive(std::chrono::milliseconds timeout) {
        using namespace std::chrono;
        const auto deadline = steady_clock::now() + timeout;
        for (;;) {
            if (link_) link_->maybe_beat(socket_);
            const auto left = duration_cast<milliseconds>(deadline - steady_clock::now());
            const auto rx = socket_.recv_from(buf_, left.count() > 0 ? left : milliseconds(0));
            if (!rx) return std::nullopt;
            last_from_ = rx->from;
            const wire::ByteView data(buf_.data(), rx->size);
            if (link_) {
                std::optional<std::vector<std::string>> unused;
                if (link_->handle(data, unused)) {
                    if (!link_->error().empty()) {
                        throw wire::WireError(wire::WireError::Kind::HubRejected,
                                              "el hub rechazó escuchar '" + topic_ + "': " +
                                                  link_->error());
                    }
                    if (steady_clock::now() >= deadline) return std::nullopt;
                    continue;
                }
            }
            const auto [env, payload] = wire::Envelope::read_from(data);
            if (env.encoding == wire::Encoding::Announcement) {
                announced_[env.schema_id] = wire::read_announcement(env, payload);
                if (steady_clock::now() >= deadline) return std::nullopt;
                continue;
            }
            if (env.schema_id != wire::schema_id_v<M>) {
                std::string detail;
                if (const auto it = announced_.find(env.schema_id); it != announced_.end()) {
                    detail = it->second.schema + ", topic " + it->second.topic;
                }
                throw wire::WireError(
                    wire::WireError::Kind::TypeMismatch,
                    wire::type_mismatch_text(wire::schema_text<M>(), wire::schema_id_v<M>,
                                             env.schema_id, detail));
            }
            auto decoded = wire::decode<M>(data);
            return Sample<M>{decoded.envelope, decoded.message, rx->from};
        }
    }

    /// Late con el hub. Llámalo si pasas más de 3 s sin llamar receive().
    void heartbeat() {
        if (link_) link_->maybe_beat(socket_);
    }

    /// "ip:puerto" del último paquete recibido (útil al reportar un error).
    [[nodiscard]] const std::string& last_from() const { return last_from_; }

    /// Anuncios vistos: schema_id → (topic, esquema).
    [[nodiscard]] const std::map<std::uint64_t, wire::Announcement>& announced() const {
        return announced_;
    }

private:
    net::UdpSocket socket_;
    std::vector<std::uint8_t> buf_;
    std::optional<hub::Link> link_;
    std::string topic_;
    std::map<std::uint64_t, wire::Announcement> announced_;
    std::string last_from_;
};

}  // namespace agmen
