#include "agmen/pubsub.hpp"

#include "agmen/clock.hpp"
#include "agmen/log.hpp"

namespace agmen {

namespace {

void check_topic_or_throw(const std::string& topic) {
    try {
        hub::check_topic(topic);
    } catch (const std::invalid_argument& e) {
        throw wire::WireError(wire::WireError::Kind::HubRejected, e.what());
    }
}

const char* clock_name(wire::Clock c) {
    return c == wire::Clock::Sim ? "simulación" : c == wire::Clock::Replay ? "reproducción" : "real";
}

}  // namespace

// ---------------------------------------------------------------------------
// PublisherCore
// ---------------------------------------------------------------------------

struct PublisherCore::State {
    TypeInfo type;
    std::mutex mu;   // se puede publicar desde varios grupos de un Node (hilos)
    net::UdpSocket socket;
    std::string topic;
    std::uint64_t topic_id = 0;
    std::uint64_t source_id = 0;
    std::vector<std::uint8_t> buf = std::vector<std::uint8_t>(65536);
    std::vector<net::Address> dests;
    std::optional<hub::Link> link;
    std::uint64_t seq = 0;
    bool announced = false;
    std::chrono::steady_clock::time_point last_announce{};

    State(const std::string& to, TypeInfo t, std::string label)
        // Por topic todo es local (nivel 2): 127.0.0.1, así Windows no pregunta por el firewall.
        : type(std::move(t)),
          socket(net::UdpSocket::bind(hub::is_topic(to) ? "127.0.0.1:0" : "0.0.0.0:0")),
          topic(hub::is_topic(to) ? to : (label.empty() ? "/" + type.name : std::move(label))),
          source_id(wire::random_source_id()) {
        if (hub::is_topic(to)) {
            check_topic_or_throw(to);
            const auto id = hub::hex16(source_id);
            link.emplace(id, hub::line("pub", {{"id", id},
                                               {"node", hub::node_name()},
                                               {"pid", hub::process_id()},
                                               {"topic", topic},
                                               {"schema", hub::hex16(type.schema_id)},
                                               {"type", type.name},
                                               {"text", type.schema_text}}));
            link->beat(socket);
            // Espera breve a la primera respuesta del hub: si ya hay receptores,
            // el primer mensaje no se pierde por no saber a quién mandarlo.
            const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(100);
            while (!link->replied() && std::chrono::steady_clock::now() < end) {
                drain(std::chrono::milliseconds(10));
            }
        } else {
            dests.push_back(net::Address::resolve(to));
        }
        topic_id = wire::topic_id(topic);
    }

    ~State() {
        if (link && socket.valid()) link->bye(socket);
    }

    wire::Bytes announcement() const {
        return wire::encode_announcement(type.schema_text, topic,
                                         wire::Envelope{.source_id = source_id,
                                                        .seq = seq,
                                                        .t_pub = wire::now_ns(),
                                                        .clock = clock::domain()});
    }

    void announce_locked() {
        const auto ann = announcement();
        for (const auto& d : dests) (void)socket.send_to(ann, d);
        announced = true;
        last_announce = std::chrono::steady_clock::now();
    }

    /// Lee las respuestas del hub (a quién mandarle los datos).
    void drain(std::chrono::milliseconds wait) {
        while (const auto rx = socket.recv_from(buf, wait)) {
            std::optional<std::vector<std::string>> subs;
            link->handle(wire::ByteView(buf.data(), rx->size), subs);
            if (subs) {
                std::vector<net::Address> fresh;
                fresh.reserve(subs->size());
                const auto ann = announcement();
                for (const auto& a : *subs) {
                    fresh.push_back(net::Address::resolve(a));
                    bool known = false;
                    for (const auto& d : dests) known = known || d == fresh.back();
                    if (!known) (void)socket.send_to(ann, fresh.back());  // recién llegado
                }
                dests = std::move(fresh);
            }
            wait = std::chrono::milliseconds(0);
        }
    }
};

PublisherCore::PublisherCore(const std::string& to, const TypeInfo& type, std::string topic)
    : s_(std::make_unique<State>(to, type, std::move(topic))) {}
PublisherCore::PublisherCore(PublisherCore&&) noexcept = default;
PublisherCore& PublisherCore::operator=(PublisherCore&&) noexcept = default;
PublisherCore::~PublisherCore() = default;

net::SendStatus PublisherCore::publish_payload(wire::ByteView payload, std::int64_t t_data) {
    auto& s = *s_;
    if (s.type.payload_size != 0 && payload.size() != s.type.payload_size) {
        throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                              wire::payload_size_text(s.type.name, s.type.payload_size, payload.size()));
    }
    std::lock_guard lock(s.mu);
    if (s.link) {
        s.link->maybe_beat(s.socket);
        s.drain(std::chrono::milliseconds(0));
        if (!s.link->error().empty()) {
            throw wire::WireError(wire::WireError::Kind::HubRejected,
                                  "el hub rechazó publicar en '" + s.topic + "': " + s.link->error());
        }
    }
    const auto now = std::chrono::steady_clock::now();
    if (!s.announced || now - s.last_announce >= kAnnounceEvery) s.announce_locked();
    wire::Bytes packet;
    packet.reserve(wire::kEnvelopeLen + payload.size());
    wire::Envelope{.encoding = wire::Encoding::Compact,
                   .schema_id = s.type.schema_id,
                   .topic_id = s.topic_id,
                   .source_id = s.source_id,
                   .seq = s.seq,
                   .t_data = t_data,
                   .t_pub = wire::now_ns(),
                   .payload_len = static_cast<std::uint32_t>(payload.size()),
                   .clock = clock::domain()}
        .write_to(packet);
    packet.insert(packet.end(), payload.begin(), payload.end());
    ++s.seq;
    auto status = net::SendStatus::NoListener;
    for (const auto& d : s.dests) {
        if (s.socket.send_to(packet, d) == net::SendStatus::Ok) status = net::SendStatus::Ok;
    }
    return status;
}

void PublisherCore::announce() {
    std::lock_guard lock(s_->mu);
    s_->announce_locked();
}

void PublisherCore::heartbeat() {
    if (!s_->link) return;
    std::lock_guard lock(s_->mu);
    s_->link->maybe_beat(s_->socket);
    s_->drain(std::chrono::milliseconds(0));
}

std::uint64_t PublisherCore::seq() const { return s_->seq; }
std::uint64_t PublisherCore::source_id() const { return s_->source_id; }
const std::string& PublisherCore::topic() const { return s_->topic; }
std::size_t PublisherCore::subscribers() const { return s_->dests.size(); }
const TypeInfo& PublisherCore::type() const { return s_->type; }

// ---------------------------------------------------------------------------
// SubscriberCore
// ---------------------------------------------------------------------------

struct SubscriberCore::State {
    TypeInfo type;
    std::string where;
    net::UdpSocket socket;
    std::vector<std::uint8_t> buf = std::vector<std::uint8_t>(65536);
    std::optional<hub::Link> link;
    std::string topic;
    std::map<std::uint64_t, wire::Announcement> announced;
    std::string last_from;
    bool clock_warned = false;
    std::mutex link_mu;   // el enlace con el hub (heartbeat() puede venir de otro hilo)

    State(const std::string& w, TypeInfo t)
        : type(std::move(t)), where(w), socket(net::UdpSocket::bind(hub::is_topic(w) ? "127.0.0.1:0" : w)) {
        if (hub::is_topic(w)) {
            check_topic_or_throw(w);
            topic = w;
            const auto id = hub::hex16(wire::random_source_id());
            link.emplace(id, hub::line("sub", {{"id", id},
                                               {"node", hub::node_name()},
                                               {"pid", hub::process_id()},
                                               {"topic", w},
                                               {"schema", hub::hex16(type.schema_id)},
                                               {"type", type.name},
                                               {"port", std::to_string(socket.local_port())}}));
            link->beat(socket);
        }
    }

    ~State() {
        if (link && socket.valid()) link->bye(socket);
    }

    /// RFC 0001 §8.8: un mensaje de otro reloj (real en un robot simulado, o al
    /// revés) se avisa una vez. No es un error: el dato llega igual.
    void warn_clock(const wire::Envelope& env) {
        if (clock_warned || env.clock == clock::domain()) return;
        clock_warned = true;
        log::write("[agmen] ⚠ '" + where + "' llega con reloj " + clock_name(env.clock) +
                   " y este nodo usa reloj " + clock_name(clock::domain()) +
                   ". ¿Corriste el nodo fuera de 'agmen run'? Usa el mismo reloj: AGMEN_CLOCK=" +
                   (env.clock == wire::Clock::Sim ? "sim" : "real"));
    }
};

SubscriberCore::SubscriberCore(const std::string& where, const TypeInfo& type)
    : s_(std::make_unique<State>(where, type)) {}
SubscriberCore::SubscriberCore(SubscriberCore&&) noexcept = default;
SubscriberCore& SubscriberCore::operator=(SubscriberCore&&) noexcept = default;
SubscriberCore::~SubscriberCore() = default;

std::optional<RawSample> SubscriberCore::receive_raw(std::chrono::milliseconds timeout) {
    using namespace std::chrono;
    auto& s = *s_;
    const auto deadline = steady_clock::now() + timeout;
    for (;;) {
        if (s.link) {
            std::lock_guard lock(s.link_mu);
            s.link->maybe_beat(s.socket);
        }
        const auto left = duration_cast<milliseconds>(deadline - steady_clock::now());
        const auto rx = s.socket.recv_from(s.buf, left.count() > 0 ? left : milliseconds(0));
        if (!rx) return std::nullopt;
        s.last_from = rx->from;
        const wire::ByteView data(s.buf.data(), rx->size);
        if (s.link) {
            std::optional<std::vector<std::string>> unused;
            std::unique_lock lock(s.link_mu);
            if (s.link->handle(data, unused)) {
                if (!s.link->error().empty()) {
                    throw wire::WireError(wire::WireError::Kind::HubRejected,
                                          "el hub rechazó escuchar '" + s.topic + "': " + s.link->error());
                }
                continue;   // si ya pasó el tiempo, la siguiente lectura no espera
            }
        }
        const auto [env, payload] = wire::Envelope::read_from(data);
        if (env.encoding == wire::Encoding::Announcement) {
            s.announced[env.schema_id] = wire::read_announcement(env, payload);
            continue;   // si ya pasó el tiempo, la siguiente lectura no espera
        }
        if (env.encoding != wire::Encoding::Compact) {
            throw wire::WireError(wire::WireError::Kind::UnknownEncoding,
                                  "se esperaba un mensaje compact y llegó encoding=" +
                                      std::to_string(static_cast<int>(env.encoding)));
        }
        if (env.schema_id != s.type.schema_id) {
            std::string detail;
            if (const auto it = s.announced.find(env.schema_id); it != s.announced.end()) {
                detail = it->second.schema + ", topic " + it->second.topic;
            }
            throw wire::WireError(wire::WireError::Kind::TypeMismatch,
                                  wire::type_mismatch_text(s.type.schema_text, s.type.schema_id,
                                                           env.schema_id, detail));
        }
        if (s.type.payload_size != 0 && payload.size() != s.type.payload_size) {
            throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                                  wire::payload_size_text(s.type.name, s.type.payload_size, payload.size()));
        }
        s.warn_clock(env);
        return RawSample{env, wire::Bytes(payload.begin(), payload.end()), rx->from};
    }
}

void SubscriberCore::heartbeat() {
    if (!s_->link) return;
    std::lock_guard lock(s_->link_mu);
    s_->link->maybe_beat(s_->socket);
}

std::uint16_t SubscriberCore::local_port() const { return s_->socket.local_port(); }

const std::string& SubscriberCore::last_from() const { return s_->last_from; }
std::intptr_t SubscriberCore::native_handle() const { return s_->socket.native_handle(); }
const std::string& SubscriberCore::where() const { return s_->where; }
const TypeInfo& SubscriberCore::type() const { return s_->type; }
const std::map<std::uint64_t, wire::Announcement>& SubscriberCore::announced() const { return s_->announced; }

}  // namespace agmen
