// agmen/pubsub.hpp — mandar y recibir un tipo de mensaje.
//
//   agmen::Publisher<MotorCommand> pub("127.0.0.1:7500", "/cmd");
//   pub.publish(MotorCommand{.left = 0.5f, .right = 0.5f});
//
//   agmen::Subscriber<MotorCommand> sub("127.0.0.1:7500");
//   if (auto s = sub.receive(std::chrono::milliseconds(50))) {
//       use(s->message, s->envelope.seq, s->from);
//   }
//
// Se encargan de lo que antes se hacía a mano: número de secuencia, marcas de
// tiempo, source_id y los anuncios de esquema (RFC 0001, anexo A). El
// Publisher manda uno antes del primer dato y luego uno por segundo, para que
// el panel y `agmen echo` puedan leer cualquier mensaje. El Subscriber los
// usa para dar mejores errores y nunca te los entrega como datos.
//
// En el nivel 2 (Agmen 0.5) recibirán un topic en vez de una dirección; el
// resto de tu código no cambia.
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "agmen/net.hpp"
#include "agmen/wire.hpp"

namespace agmen {

/// Cada cuánto el Publisher repite el anuncio de su esquema.
inline constexpr std::chrono::milliseconds kAnnounceEvery{1000};

template <wire::Message M>
class Publisher {
public:
    /// `to` = "ip:puerto" del receptor. `topic` = nombre lógico del flujo
    /// (default "/" + kName); viaja en el sobre como topic_id.
    explicit Publisher(const std::string& to, std::string topic = "/" + std::string(M::kName))
        : socket_(net::UdpSocket::bind("0.0.0.0:0")),
          topic_(std::move(topic)),
          topic_id_(wire::topic_id(topic_)),
          source_id_(wire::random_source_id()),
          schema_text_(wire::schema_text<M>()) {
        AGMEN_CHECK_SCHEMA(M);
        socket_.connect(to);
    }

    /// Manda `msg` con t_data = ahora. NoListener = nadie escucha (no es error).
    net::SendStatus publish(const M& msg) { return publish(msg, wire::now_ns()); }

    /// Manda `msg`; t_data = instante al que corresponden los datos (ej. la
    /// captura del sensor), en ns desde la época Unix.
    net::SendStatus publish(const M& msg, std::int64_t t_data) {
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
        return socket_.send(packet);
    }

    /// Manda el anuncio del esquema ya (publish() lo hace solo).
    void announce() {
        (void)socket_.send(wire::encode_announcement(
            schema_text_, topic_,
            wire::Envelope{.source_id = source_id_, .seq = seq_, .t_pub = wire::now_ns()}));
        announced_ = true;
        last_announce_ = std::chrono::steady_clock::now();
    }

    [[nodiscard]] std::uint64_t seq() const { return seq_; }
    [[nodiscard]] std::uint64_t source_id() const { return source_id_; }
    [[nodiscard]] const std::string& topic() const { return topic_; }

private:
    net::UdpSocket socket_;
    std::string topic_;
    std::uint64_t topic_id_;
    std::uint64_t source_id_;
    std::string schema_text_;
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
    /// `listen` = "ip:puerto" donde escuchar (ej. "127.0.0.1:7500").
    explicit Subscriber(const std::string& listen)
        : socket_(net::UdpSocket::bind(listen)), buf_(65536) {
        AGMEN_CHECK_SCHEMA(M);
    }

    /// Espera hasta `timeout` un mensaje de tipo M. std::nullopt si no llegó.
    /// Lanza wire::WireError si llega algo inválido o de otro tipo.
    [[nodiscard]] std::optional<Sample<M>> receive(std::chrono::milliseconds timeout) {
        using namespace std::chrono;
        const auto deadline = steady_clock::now() + timeout;
        for (;;) {
            const auto left = duration_cast<milliseconds>(deadline - steady_clock::now());
            const auto rx = socket_.recv_from(buf_, left.count() > 0 ? left : milliseconds(0));
            if (!rx) return std::nullopt;
            last_from_ = rx->from;
            const wire::ByteView data(buf_.data(), rx->size);
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

    /// "ip:puerto" del último paquete recibido (útil al reportar un error).
    [[nodiscard]] const std::string& last_from() const { return last_from_; }

    /// Anuncios vistos: schema_id → (topic, esquema).
    [[nodiscard]] const std::map<std::uint64_t, wire::Announcement>& announced() const {
        return announced_;
    }

private:
    net::UdpSocket socket_;
    std::vector<std::uint8_t> buf_;
    std::map<std::uint64_t, wire::Announcement> announced_;
    std::string last_from_;
};

}  // namespace agmen
