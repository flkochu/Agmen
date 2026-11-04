#include "agmen/pubsub.hpp"

#include <atomic>
#include <cstring>
#include <deque>
#include <thread>

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

constexpr std::chrono::milliseconds kConnectTimeout{1000};
constexpr std::chrono::milliseconds kRetryEvery{1000};
constexpr std::size_t kReadChunk = 1 << 18;

using Packet = std::shared_ptr<const wire::Bytes>;

// ---------------------------------------------------------------------------
// TCP del lado del Publisher (RFC 0008 D2, D3)
// ---------------------------------------------------------------------------

/// Una conexión a un suscriptor. publish() deja aquí el mensaje; el hilo del
/// Fanout lo manda sin bloquear a nadie.
struct Outgoing {
    net::TcpStream stream;
    Packet current;          // el paquete en curso
    std::size_t pos = 0;     // cuánto de current ya salió
    Packet next;             // el siguiente (solo el más nuevo)
    std::uint64_t dropped = 0;
    std::uint64_t sent = 0;

    [[nodiscard]] bool alive() const { return stream.valid(); }
    [[nodiscard]] bool busy() const { return current || next; }

    void offer(Packet p) {
        if (!current) {
            current = std::move(p);
            pos = 0;
        } else {
            if (next) ++dropped;   // el suscriptor va lento: se queda con el más nuevo (D3)
            next = std::move(p);
        }
    }

    /// Manda lo que acepte el sistema ahora.
    void step() {
        while (stream.valid() && current) {
            const auto n = stream.send_some(wire::ByteView(current->data() + pos, current->size() - pos));
            if (n < 0) {
                current.reset();
                next.reset();
                return;
            }
            if (n == 0) return;
            pos += static_cast<std::size_t>(n);
            if (pos < current->size()) continue;
            current.reset();
            pos = 0;
            ++sent;
            if (next) current = std::move(next);
        }
    }
};

/// Las conexiones de un Publisher a sus suscriptores, y el hilo que las atiende.
class Fanout {
public:
    Fanout() : wake_(net::UdpSocket::bind("127.0.0.1:0")) {
        wake_.connect("127.0.0.1:" + std::to_string(wake_.local_port()));
        thread_ = std::thread([this] { loop(); });
    }

    ~Fanout() { close(); }

    void close() {
        if (closed_.exchange(true)) return;
        {
            std::lock_guard lock(mu_);
            conns_.clear();
            want_.clear();
        }
        wake();
        if (thread_.joinable()) thread_.join();
    }

    /// La lista nueva de suscriptores (del hub o la dirección fija).
    void update(std::vector<std::string> addrs, Packet announcement) {
        {
            std::lock_guard lock(mu_);
            want_ = std::move(addrs);
            announcement_ = std::move(announcement);
            for (auto it = conns_.begin(); it != conns_.end();) {
                if (std::find(want_.begin(), want_.end(), it->first) == want_.end()) {
                    last_try_.erase(it->first);
                    it = conns_.erase(it);
                } else {
                    ++it;
                }
            }
            reconnect(true);
        }
        wake();
    }

    /// Deja `packet` en cada conexión y despierta al hilo. True si hay alguien.
    bool send(Packet packet) {
        bool ok = false;
        bool busy = false;
        {
            std::lock_guard lock(mu_);
            reconnect(false);
            for (auto it = conns_.begin(); it != conns_.end();) {
                if (!it->second.alive()) {
                    it = conns_.erase(it);
                    continue;
                }
                it->second.offer(packet);
                it->second.step();   // casi siempre sale entero de una vez
                busy = busy || it->second.busy();
                ok = true;
                ++it;
            }
        }
        if (busy) wake();
        return ok;
    }

    void flush() { wake(); }

    [[nodiscard]] std::uint64_t dropped() const {
        std::lock_guard lock(mu_);
        std::uint64_t n = 0;
        for (const auto& [_, c] : conns_) n += c.dropped;
        return n + dropped_gone_;
    }

    [[nodiscard]] std::size_t connected() const {
        std::lock_guard lock(mu_);
        std::size_t n = 0;
        for (const auto& [_, c] : conns_) {
            if (c.alive()) ++n;
        }
        return n;
    }

private:
    mutable std::mutex mu_;
    std::map<std::string, Outgoing> conns_;
    std::vector<std::string> want_;
    Packet announcement_;
    std::map<std::string, std::chrono::steady_clock::time_point> last_try_;
    std::uint64_t dropped_gone_ = 0;   // de conexiones que ya se cerraron
    net::UdpSocket wake_;              // un byte aquí despierta al hilo (funciona igual en Windows)
    std::thread thread_;
    std::atomic<bool> closed_{false};

    void wake() {
        const std::uint8_t b = 0;
        (void)wake_.send(wire::ByteView(&b, 1));
    }

    void reconnect(bool force) {
        const auto now = std::chrono::steady_clock::now();
        for (const auto& a : want_) {
            if (const auto it = conns_.find(a); it != conns_.end() && it->second.alive()) continue;
            if (!force) {
                if (const auto t = last_try_.find(a); t != last_try_.end() && now - t->second < kRetryEvery) continue;
            }
            last_try_[a] = now;
            if (const auto it = conns_.find(a); it != conns_.end()) {
                dropped_gone_ += it->second.dropped;
                conns_.erase(it);
            }
            try {
                Outgoing c;
                c.stream = net::TcpStream::connect(net::Address::resolve(a), kConnectTimeout);
                if (announcement_) {
                    c.offer(announcement_);
                    c.step();
                }
                conns_.emplace(a, std::move(c));
            } catch (const net::NetError&) {
                // el suscriptor no está; se reintenta en un rato (o el hub lo quita)
            }
        }
    }

    void loop() {
        std::vector<std::uint8_t> drain(64);
        while (!closed_) {
            std::vector<std::intptr_t> handles{wake_.native_handle()};
            std::vector<Outgoing*> busy;
            {
                std::lock_guard lock(mu_);
                for (auto& [_, c] : conns_) {
                    if (c.alive() && c.busy()) {
                        busy.push_back(&c);
                        handles.push_back(c.stream.native_handle());
                    }
                }
            }
            std::unique_ptr<bool[]> ready(new bool[handles.size()]());
            int n = 0;
            try {
                n = net::wait_io(handles, 1, std::chrono::milliseconds(busy.empty() ? 1000 : 500),
                                 std::span<bool>(ready.get(), handles.size()));
            } catch (const net::NetError&) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            if (n <= 0) continue;
            if (ready[0]) (void)wake_.recv_from(drain, std::chrono::milliseconds(0));
            std::lock_guard lock(mu_);
            for (std::size_t i = 1; i < handles.size(); ++i) {
                if (!ready[i]) continue;
                // La conexión puede haberse cerrado entre la lista y ahora: se busca de nuevo.
                for (auto& [_, c] : conns_) {
                    if (c.stream.native_handle() == handles[i]) c.step();
                }
            }
        }
    }
};

// ---------------------------------------------------------------------------
// TCP del lado del Subscriber
// ---------------------------------------------------------------------------

/// Una conexión aceptada: junta bytes hasta tener un paquete completo.
struct Incoming {
    net::TcpStream stream;
    wire::Bytes head;        // los primeros 64 bytes
    wire::Bytes packet;      // el paquete completo en construcción
    std::size_t filled = 0;  // cuánto de packet ya llegó (0 = todavía se junta head)
    std::vector<std::uint8_t> chunk = std::vector<std::uint8_t>(kReadChunk);

    /// Lee lo que haya; deja en `out` los paquetes completos. false si se cerró.
    bool read(std::deque<std::pair<wire::Bytes, std::string>>& out) {
        if (!packet.empty()) {
            // Ya sabemos cuánto mide: se lee directo en su lugar.
            const auto n = stream.recv_some(std::span<std::uint8_t>(packet.data() + filled, packet.size() - filled));
            if (n < 0) return false;
            filled += static_cast<std::size_t>(n);
            if (filled == packet.size()) {
                out.emplace_back(std::move(packet), stream.peer());
                packet.clear();
                filled = 0;
            }
            return true;
        }
        const auto n = stream.recv_some(chunk);
        if (n < 0) return false;
        feed(wire::ByteView(chunk.data(), static_cast<std::size_t>(n)), out);
        return true;
    }

    void feed(wire::ByteView data, std::deque<std::pair<wire::Bytes, std::string>>& out) {
        while (!data.empty()) {
            if (packet.empty()) {
                const std::size_t need = wire::kEnvelopeLen - head.size();
                const std::size_t take = std::min(need, data.size());
                head.insert(head.end(), data.begin(), data.begin() + static_cast<std::ptrdiff_t>(take));
                data = data.subspan(take);
                if (head.size() < wire::kEnvelopeLen) return;
                if (std::memcmp(head.data(), "AGM", 3) != 0) {
                    throw wire::WireError(wire::WireError::Kind::BadMagic,
                                          "magic inválido en la conexión de " + stream.peer() +
                                              ": el paquete no viene de un nodo de Agmen");
                }
                std::uint16_t header_len = 0;
                std::uint32_t payload_len = 0;
                std::memcpy(&header_len, head.data() + 4, 2);
                std::memcpy(&payload_len, head.data() + 56, 4);
                const std::size_t total = std::size_t{header_len} + payload_len;
                if (total > wire::max_message() + wire::kEnvelopeLen) {
                    throw wire::WireError(wire::WireError::Kind::TooBig, wire::too_big_text(stream.peer(), total));
                }
                packet.resize(std::max(total, head.size()));
                std::memcpy(packet.data(), head.data(), head.size());
                filled = head.size();
                head.clear();
            }
            const std::size_t room = packet.size() - filled;
            const std::size_t take = std::min(room, data.size());
            std::memcpy(packet.data() + filled, data.data(), take);
            filled += take;
            data = data.subspan(take);
            if (filled == packet.size()) {
                out.emplace_back(std::move(packet), stream.peer());
                packet.clear();
                filled = 0;
            }
        }
    }
};

struct Listener {
    net::TcpListener listener;
    std::vector<std::unique_ptr<Incoming>> conns;
    std::deque<std::pair<wire::Bytes, std::string>> ready;

    explicit Listener(std::string_view bind) : listener(net::TcpListener::bind(bind)) {}

    void handles(std::vector<std::intptr_t>& out) const {
        out.push_back(listener.native_handle());
        for (const auto& c : conns) out.push_back(c->stream.native_handle());
    }

    /// Atiende los sockets listos: acepta y lee.
    void service(const std::vector<std::intptr_t>& handles, const bool* is_ready, std::size_t from) {
        if (is_ready[from]) {
            while (auto s = listener.accept()) {
                auto c = std::make_unique<Incoming>();
                c->stream = std::move(*s);
                conns.push_back(std::move(c));
            }
        }
        for (std::size_t i = from + 1; i < handles.size(); ++i) {
            if (!is_ready[i]) continue;
            const auto it = std::find_if(conns.begin(), conns.end(),
                                         [&](const auto& c) { return c->stream.native_handle() == handles[i]; });
            if (it == conns.end()) continue;
            bool alive = true;
            try {
                alive = (*it)->read(ready);
            } catch (...) {
                conns.erase(it);
                throw;
            }
            if (!alive) conns.erase(it);
        }
    }
};

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
    std::vector<std::string> tcp_dests;
    std::unique_ptr<Fanout> fanout;   // solo tipos arrow
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
        if (type.arrow) fanout = std::make_unique<Fanout>();
        topic_id = wire::topic_id(topic);
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
        } else if (fanout) {
            tcp_dests.push_back(to);
            fanout->update(tcp_dests, std::make_shared<const wire::Bytes>(announcement()));
        } else {
            dests.push_back(net::Address::resolve(to));
        }
    }

    ~State() {
        if (fanout) fanout->close();
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
        if (fanout) {
            fanout->send(std::make_shared<const wire::Bytes>(ann));
        } else {
            for (const auto& d : dests) (void)socket.send_to(ann, d);
        }
        announced = true;
        last_announce = std::chrono::steady_clock::now();
    }

    /// Lee las respuestas del hub (a quién mandarle los datos).
    void drain(std::chrono::milliseconds wait) {
        while (const auto rx = socket.recv_from(buf, wait)) {
            std::optional<std::vector<std::string>> subs;
            std::optional<std::vector<std::string>> tcp;
            link->handle(wire::ByteView(buf.data(), rx->size), subs, &tcp);
            if (subs && fanout) {
                tcp_dests = tcp ? *tcp : std::vector<std::string>{};
                fanout->update(tcp_dests, std::make_shared<const wire::Bytes>(announcement()));
            } else if (subs) {
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
    if (!s.type.arrow && s.type.payload_size != 0 && payload.size() != s.type.payload_size) {
        throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                              wire::payload_size_text(s.type.name, s.type.payload_size, payload.size()));
    }
    if (s.type.arrow && payload.size() > wire::max_message()) {
        throw wire::WireError(wire::WireError::Kind::TooBig, wire::too_big_text(s.topic, payload.size()));
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
    auto packet = std::make_shared<wire::Bytes>();
    packet->reserve(wire::kEnvelopeLen + payload.size());
    wire::Envelope{.encoding = s.type.arrow ? wire::Encoding::Arrow : wire::Encoding::Compact,
                   .schema_id = s.type.schema_id,
                   .topic_id = s.topic_id,
                   .source_id = s.source_id,
                   .seq = s.seq,
                   .t_data = t_data,
                   .t_pub = wire::now_ns(),
                   .payload_len = static_cast<std::uint32_t>(payload.size()),
                   .clock = clock::domain()}
        .write_to(*packet);
    packet->insert(packet->end(), payload.begin(), payload.end());
    ++s.seq;
    if (s.fanout) return s.fanout->send(packet) ? net::SendStatus::Ok : net::SendStatus::NoListener;
    auto status = net::SendStatus::NoListener;
    for (const auto& d : s.dests) {
        if (s.socket.send_to(*packet, d) == net::SendStatus::Ok) status = net::SendStatus::Ok;
    }
    return status;
}

void PublisherCore::announce() {
    std::lock_guard lock(s_->mu);
    s_->announce_locked();
}

void PublisherCore::heartbeat() {
    if (s_->fanout) s_->fanout->flush();
    if (!s_->link) return;
    std::lock_guard lock(s_->mu);
    s_->link->maybe_beat(s_->socket);
    s_->drain(std::chrono::milliseconds(0));
}

std::uint64_t PublisherCore::seq() const { return s_->seq; }
std::uint64_t PublisherCore::source_id() const { return s_->source_id; }
const std::string& PublisherCore::topic() const { return s_->topic; }
std::size_t PublisherCore::subscribers() const { return s_->fanout ? s_->tcp_dests.size() : s_->dests.size(); }
const TypeInfo& PublisherCore::type() const { return s_->type; }
std::uint64_t PublisherCore::dropped() const { return s_->fanout ? s_->fanout->dropped() : 0; }

// ---------------------------------------------------------------------------
// SubscriberCore
// ---------------------------------------------------------------------------

struct SubscriberCore::State {
    TypeInfo type;
    std::string where;
    net::UdpSocket socket;
    std::unique_ptr<Listener> tcp;   // solo tipos arrow
    std::vector<std::uint8_t> buf = std::vector<std::uint8_t>(65536);
    std::optional<hub::Link> link;
    std::string topic;
    std::map<std::uint64_t, wire::Announcement> announced;
    std::string last_from;
    bool clock_warned = false;
    std::mutex link_mu;   // el enlace con el hub (heartbeat() puede venir de otro hilo)

    State(const std::string& w, TypeInfo t)
        : type(std::move(t)), where(w), socket(net::UdpSocket::bind(hub::is_topic(w) ? "127.0.0.1:0" : w)) {
        if (type.arrow) {
            // Los mensajes arrow llegan por TCP: con dirección fija, el mismo puerto.
            tcp = std::make_unique<Listener>(hub::is_topic(w) ? std::string("127.0.0.1:0")
                                                              : "127.0.0.1:" + std::to_string(socket.local_port()));
        }
        if (hub::is_topic(w)) {
            check_topic_or_throw(w);
            topic = w;
            const auto id = hub::hex16(wire::random_source_id());
            std::vector<std::pair<std::string, std::string>> fields{{"id", id},
                                                                    {"node", hub::node_name()},
                                                                    {"pid", hub::process_id()},
                                                                    {"topic", w},
                                                                    {"schema", hub::hex16(type.schema_id)},
                                                                    {"type", type.name},
                                                                    {"port", std::to_string(socket.local_port())}};
            if (tcp) fields.emplace_back("tcp", std::to_string(tcp->listener.local_port()));
            link.emplace(id, hub::line("sub", fields));
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

    /// Un paquete de datos (de UDP o de TCP) → la muestra, o nullopt si era un
    /// anuncio. Lanza WireError si no es válido o es de otro tipo.
    std::optional<RawSample> handle_packet(wire::ByteView data, std::string from) {
        last_from = from;
        const auto [env, payload] = wire::Envelope::read_from(data);
        if (env.encoding == wire::Encoding::Announcement) {
            announced[env.schema_id] = wire::read_announcement(env, payload);
            return std::nullopt;
        }
        const auto want = type.arrow ? wire::Encoding::Arrow : wire::Encoding::Compact;
        if (env.encoding != want) {
            throw wire::WireError(wire::WireError::Kind::UnknownEncoding,
                                  std::string("se esperaba un mensaje ") + (type.arrow ? "arrow" : "compact") +
                                      " y llegó encoding=" + std::to_string(static_cast<int>(env.encoding)));
        }
        if (env.schema_id != type.schema_id) {
            std::string detail;
            if (const auto it = announced.find(env.schema_id); it != announced.end()) {
                detail = it->second.schema + ", topic " + it->second.topic;
            }
            throw wire::WireError(wire::WireError::Kind::TypeMismatch,
                                  wire::type_mismatch_text(type.schema_text, type.schema_id, env.schema_id, detail));
        }
        if (!type.arrow && type.payload_size != 0 && payload.size() != type.payload_size) {
            throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                                  wire::payload_size_text(type.name, type.payload_size, payload.size()));
        }
        warn_clock(env);
        return RawSample{env, wire::Bytes(payload.begin(), payload.end()), std::move(from)};
    }

    /// Un datagrama UDP: control del hub, o datos.
    std::optional<RawSample> handle_datagram(wire::ByteView data, std::string from) {
        if (link) {
            std::optional<std::vector<std::string>> unused;
            std::unique_lock lock(link_mu);
            if (link->handle(data, unused)) {
                if (!link->error().empty()) {
                    throw wire::WireError(wire::WireError::Kind::HubRejected,
                                          "el hub rechazó escuchar '" + topic + "': " + link->error());
                }
                return std::nullopt;
            }
        }
        return handle_packet(data, std::move(from));
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
        const auto wait = left.count() > 0 ? left : milliseconds(0);
        if (!s.tcp) {
            const auto rx = s.socket.recv_from(s.buf, wait);
            if (!rx) return std::nullopt;
            if (auto out = s.handle_datagram(wire::ByteView(s.buf.data(), rx->size), rx->from)) return out;
            continue;   // control o anuncio: si ya pasó el tiempo, la siguiente lectura no espera
        }
        // Tipo arrow: UDP (control y anuncios) y TCP (los datos) a la vez.
        if (!s.tcp->ready.empty()) {
            auto [packet, from] = std::move(s.tcp->ready.front());
            s.tcp->ready.pop_front();
            if (auto out = s.handle_packet(packet, std::move(from))) return out;
            continue;
        }
        std::vector<std::intptr_t> handles{s.socket.native_handle()};
        s.tcp->handles(handles);
        std::unique_ptr<bool[]> ready(new bool[handles.size()]());
        const int n = net::wait_readable(handles, wait, std::span<bool>(ready.get(), handles.size()));
        if (n <= 0) return std::nullopt;
        if (ready[0]) {
            if (const auto rx = s.socket.recv_from(s.buf, milliseconds(0))) {
                if (auto out = s.handle_datagram(wire::ByteView(s.buf.data(), rx->size), rx->from)) return out;
            }
        }
        s.tcp->service(handles, ready.get(), 1);
    }
}

void SubscriberCore::heartbeat() {
    if (!s_->link) return;
    std::lock_guard lock(s_->link_mu);
    s_->link->maybe_beat(s_->socket);
}

std::uint16_t SubscriberCore::local_port() const { return s_->socket.local_port(); }
std::uint16_t SubscriberCore::tcp_port() const { return s_->tcp ? s_->tcp->listener.local_port() : 0; }

const std::string& SubscriberCore::last_from() const { return s_->last_from; }
std::intptr_t SubscriberCore::native_handle() const { return s_->socket.native_handle(); }
std::vector<std::intptr_t> SubscriberCore::native_handles() const {
    std::vector<std::intptr_t> out{s_->socket.native_handle()};
    if (s_->tcp) s_->tcp->handles(out);
    return out;
}
const std::string& SubscriberCore::where() const { return s_->where; }
const TypeInfo& SubscriberCore::type() const { return s_->type; }
const std::map<std::uint64_t, wire::Announcement>& SubscriberCore::announced() const { return s_->announced; }

}  // namespace agmen
