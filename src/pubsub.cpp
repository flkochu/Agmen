#include "agmen/pubsub.hpp"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <deque>
#include <functional>
#include <set>
#include <thread>

#include "agmen/clock.hpp"
#include "agmen/log.hpp"
#include "agmen/platform.hpp"
#include "agmen/shm.hpp"

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

/// "0.0.0.0:7500" → "0.0.0.0" (la IP de una dirección).
std::string host_of(const std::string& address) {
    const auto colon = address.rfind(':');
    return colon == std::string::npos ? address : address.substr(0, colon);
}

// ---------------------------------------------------------------------------
// Modo red (RFC 0011 §2 y §3): lo mismo que python/agmen/pubsub.py y _tcp.py
// ---------------------------------------------------------------------------

/// Conectar por TCP a otra computadora espera hasta esto, sin frenar a nadie (a 127.0.0.1, kConnectTimeout).
// DECISIÓN PENDIENTE: 3 s, como REMOTE_CONNECT_S en Python (la especificación no da el número).
constexpr std::chrono::milliseconds kRemoteConnect{3000};
/// Tras la confirmación del hub, los sockets de antes se siguen leyendo esto (NET_OLD_S en Python).
constexpr std::chrono::milliseconds kNetOld{1000};
/// IP distintas que se avisan como rechazadas (las demás solo se cuentan).
constexpr std::size_t kMaxRejectedWarned = 256;

/// AGMEN_NET_IP (variable para pruebas, RFC 0011 §2): la IP de "esta computadora" para tener dos en una
/// (127.0.0.1 y 127.0.0.2). "" si no está. Inválida o que no es de esta computadora: error claro al abrir.
std::string net_ip_from_env() {
    std::string text = platform::env("AGMEN_NET_IP");
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    text = text.substr(first, text.find_last_not_of(" \t\r\n") - first + 1);
    try {
        (void)hub::parse_ipv4(text);
    } catch (const std::invalid_argument& e) {
        throw std::invalid_argument("AGMEN_NET_IP=" + text + ": " + e.what() +
                                    ". Es una variable para pruebas: quítala si no la pusiste tú");
    }
    try {
        (void)net::UdpSocket::bind(text + ":0");
    } catch (const net::NetError& e) {
        throw net::NetError(0, "AGMEN_NET_IP=" + text + " no es una IP de esta computadora (" + e.code().message() +
                                   "): pon una que tenga (para pruebas, una de 127.0.0.x) o quita la variable");
    }
    return text;
}

/// El aviso (una vez por destino) de un destino de otra computadora al que no se pudo mandar.
std::string unreachable_text(const std::string& topic, const std::string& addr, const std::string& why) {
    const auto ip = host_of(addr);
    return "[agmen] ⚠ '" + topic + "': no pude mandar a la computadora " + ip + " (" + addr + "): " + why +
           ". Los demás destinos siguen igual; revisa que " + ip +
           " esté en la red y que su firewall deje pasar a Agmen. Lo que no sale se cuenta (unreachable).";
}

/// ¿La IP de `from` ("ip:puerto" o "ip") está en `allowed`?
bool ip_in(std::string_view from, const std::vector<std::string>& allowed) {
    const auto colon = from.rfind(':');
    const auto ip = colon == std::string_view::npos ? from : from.substr(0, colon);
    for (const auto& a : allowed) {
        if (a == ip) return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// TCP del lado del Publisher (RFC 0008 D2, D3) y memoria compartida (RFC 0009)
// ---------------------------------------------------------------------------

/// Un mensaje en la fila de una conexión. Si va por memoria compartida, `slot`
/// dice qué bloque tiene apartado (y hay que soltarlo si no sale).
struct Item {
    Packet bytes;
    std::int64_t slot = -1;
    std::uint64_t generation = 0;
    std::size_t length = 0;   // bytes del payload en el bloque (memoria compartida)

    explicit operator bool() const { return static_cast<bool>(bytes); }
};

/// Una conexión a un suscriptor. publish() deja aquí el mensaje; el hilo del
/// Fanout lo manda sin bloquear a nadie y lee los avisos del suscriptor.
struct Outgoing {
    enum class Shm { No, Probing, Yes };

    net::TcpStream stream;
    Item current;                 // el paquete en curso
    std::size_t pos = 0;          // cuánto de current ya salió
    std::deque<Packet> control;   // la prueba: sale antes que los datos y nunca se descarta
    Item next;                    // el siguiente (solo el más nuevo)
    std::uint64_t dropped = 0;
    std::uint64_t sent = 0;
    // Memoria compartida (RFC 0009)
    Shm shm = Shm::No;
    bool hello = false;
    std::map<std::pair<std::uint32_t, std::uint64_t>, std::size_t> lent;   // mandados y no devueltos → bytes
    std::size_t lent_bytes = 0;
    wire::Bytes inbox;            // avisos a medio llegar
    // Modo red (RFC 0011 §3): de otra computadora, sin memoria compartida y sin bloquear al conectar.
    std::string addr;             // "ip:puerto" del suscriptor
    bool remote = false;
    bool connecting = false;      // conexión en curso (la termina el hilo del Fanout)
    std::chrono::steady_clock::time_point deadline{};

    [[nodiscard]] bool alive() const { return stream.valid(); }
    [[nodiscard]] bool busy() const { return current || next || !control.empty(); }
    /// Un descriptor sale solo si el suscriptor no tiene ya demasiados sin
    /// devolver (8 y 128 MiB; siempre al menos dos, D6).
    [[nodiscard]] bool can_send(const Item& it) const {
        return it.slot < 0 || lent.size() < 2 ||
               (lent.size() < shm::kMaxUnreturned && lent_bytes + it.length <= shm::kMaxUnreturnedBytes);
    }
    [[nodiscard]] bool wants_write() const {
        if (connecting) return true;   // la espera avisa cuando termina de conectar
        return current || !control.empty() || (next && can_send(next));
    }

    static void give_back(Item& it, shm::Pool* pool) {
        if (it.slot >= 0 && pool != nullptr) pool->release(static_cast<std::uint32_t>(it.slot));
        it = Item{};
    }

    void offer(Item it, shm::Pool* pool) {
        // Nada se adelanta a lo que ya espera en `next`: el orden se respeta.
        if (!current && !next && control.empty() && can_send(it)) {
            current = std::move(it);
            pos = 0;
            return;
        }
        if (next) {
            ++dropped;   // el suscriptor va lento: se queda con el más nuevo (RFC 0008 D3)
            give_back(next, pool);
        }
        next = std::move(it);
    }

    /// Manda lo que acepte el sistema ahora.
    void step(shm::Pool* pool) {
        while (stream.valid() && !connecting) {
            if (!current) {
                if (!control.empty()) {
                    current = Item{control.front()};
                    control.pop_front();
                } else if (next && can_send(next)) {
                    current = std::move(next);
                    next = Item{};
                } else {
                    return;
                }
                pos = 0;
            }
            const auto& bytes = *current.bytes;
            const auto n = stream.send_some(wire::ByteView(bytes.data() + pos, bytes.size() - pos));
            if (n < 0) {
                fail(pool);
                return;
            }
            if (n == 0) return;
            pos += static_cast<std::size_t>(n);
            if (pos < bytes.size()) continue;
            if (current.slot >= 0) {
                lent.emplace(std::pair{static_cast<std::uint32_t>(current.slot), current.generation}, current.length);
                lent_bytes += current.length;
            }
            current = Item{};
            pos = 0;
            ++sent;
        }
    }

    /// La conexión se cerró: todo lo que tenía apartado vuelve al pool.
    void fail(shm::Pool* pool) {
        stream.close();
        give_back(current, pool);
        give_back(next, pool);
        if (pool != nullptr) {
            for (const auto& [key, bytes] : lent) pool->release(key.first);
        }
        lent.clear();
        lent_bytes = 0;
        control.clear();
        shm = Shm::No;
    }
};

/// Las conexiones de un Publisher a sus suscriptores, el pool de memoria
/// compartida y el hilo que las atiende.
class Fanout {
public:
    /// `net`: los destinos que no son 127.0.0.1 son de otra computadora (modo topic, RFC 0011 §3);
    /// `net_ip`: AGMEN_NET_IP, la IP desde la que salen (solo pruebas).
    Fanout(std::string topic, std::uint64_t source_id, std::uint64_t topic_id, bool net = false,
           std::string net_ip = {})
        : topic_(std::move(topic)),
          source_id_(source_id),
          topic_id_(topic_id),
          net_(net),
          net_ip_(std::move(net_ip)),
          wake_(net::UdpSocket::bind("127.0.0.1:0")) {
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
        pool_.reset();   // borra los bloques (los suscriptores que aún los ven los siguen viendo)
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
                    it = forget(it);
                } else {
                    ++it;
                }
            }
            reconnect(true, false);
        }
        wake();
    }

    /// Deja un paquete ya armado (un anuncio) en cada conexión.
    bool send_packet(Packet packet) {
        bool ok = false;
        bool wake_up = false;
        {
            std::lock_guard lock(mu_);
            reconnect(false, false);
            for (auto it = conns_.begin(); it != conns_.end();) {
                if (!it->second.alive()) {
                    it = forget(it);
                    continue;
                }
                it->second.offer(Item{packet}, pool_.get());
                it->second.step(pool_.get());
                wake_up = wake_up || it->second.wants_write();
                ok = true;
                ++it;
            }
        }
        if (wake_up) wake();
        return ok;
    }

    /// Un mensaje de datos: a cada suscriptor, por memoria compartida si puede
    /// (el payload se escribe una sola vez) y si no, entero por TCP.
    bool send(const wire::Envelope& env, wire::ByteView payload) {
        bool ok = false;
        bool wake_up = false;
        {
            std::lock_guard lock(mu_);
            reconnect(false, true);
            // D3: el mensaje nuevo reemplaza al que esperaba. Se suelta antes de
            // escribir, así su bloque se puede reutilizar para este.
            for (auto& [_, c] : conns_) {
                if (c.alive() && c.next) {
                    ++c.dropped;
                    Outgoing::give_back(c.next, pool_.get());
                }
            }
            Packet full;
            Packet described;
            std::optional<shm::Descriptor> desc;
            bool tried = false;
            for (auto it = conns_.begin(); it != conns_.end();) {
                auto& c = it->second;
                if (!c.alive()) {
                    it = forget(it);
                    continue;
                }
                Item item;
                if (c.shm == Outgoing::Shm::Yes && payload.size() >= shm::kMinPayload) {
                    if (!tried) {
                        tried = true;
                        desc = write_shared(env, payload);
                        if (desc) described = descriptor_packet(env, *desc);
                    }
                    if (desc) {
                        pool_->hold(desc->slot);
                        item = Item{described, desc->slot, desc->generation, static_cast<std::size_t>(desc->length)};
                    }
                }
                if (!item) {
                    if (!full) full = full_packet(env, payload);
                    item = Item{full};
                }
                c.offer(std::move(item), pool_.get());
                c.step(pool_.get());   // casi siempre sale entero de una vez
                wake_up = wake_up || c.wants_write();
                ok = true;
                ++it;
            }
        }
        if (wake_up) wake();
        return ok;
    }

    void flush() { wake(); }

    [[nodiscard]] std::uint64_t dropped() const {
        std::lock_guard lock(mu_);
        std::uint64_t n = 0;
        for (const auto& [_, c] : conns_) n += c.dropped;
        return n + dropped_gone_;
    }

    [[nodiscard]] std::size_t shm_subscribers() const {
        std::lock_guard lock(mu_);
        return static_cast<std::size_t>(std::count_if(conns_.begin(), conns_.end(), [](const auto& kv) {
            return kv.second.alive() && kv.second.shm == Outgoing::Shm::Yes;
        }));
    }

    [[nodiscard]] std::uint64_t shm_fallbacks() const {
        std::lock_guard lock(mu_);
        return fallbacks_;
    }

    [[nodiscard]] std::size_t shm_bytes() const {
        std::lock_guard lock(mu_);
        return pool_ ? pool_->bytes() : 0;
    }

    /// Mensajes que no salieron hacia un destino de otra computadora (RFC 0011 §3).
    [[nodiscard]] std::uint64_t unreachable() const {
        std::lock_guard lock(mu_);
        return unreachable_;
    }

private:
    std::string topic_;
    std::uint64_t source_id_;
    std::uint64_t topic_id_;
    bool net_ = false;
    std::string net_ip_;
    std::uint64_t unreachable_ = 0;            // mensajes que no salieron hacia otra computadora
    std::set<std::string> unreach_warned_;
    mutable std::mutex mu_;
    std::map<std::string, Outgoing> conns_;
    std::vector<std::string> want_;
    Packet announcement_;
    std::map<std::string, std::chrono::steady_clock::time_point> last_try_;
    std::uint64_t dropped_gone_ = 0;   // de conexiones que ya se cerraron
    std::unique_ptr<shm::Pool> pool_;  // se crea cuando un suscriptor dice que puede
    std::uint64_t fallbacks_ = 0;
    bool fallback_warned_ = false;
    net::UdpSocket wake_;              // un byte aquí despierta al hilo (funciona igual en Windows)
    std::thread thread_;
    std::atomic<bool> closed_{false};

    void wake() {
        const std::uint8_t b = 0;
        (void)wake_.send(wire::ByteView(&b, 1));
    }

    using Conns = std::map<std::string, Outgoing>;

    /// Quita una conexión y devuelve al pool lo que tenía apartado.
    Conns::iterator forget(Conns::iterator it) {
        dropped_gone_ += it->second.dropped;
        it->second.fail(pool_.get());
        return conns_.erase(it);
    }

    static Packet full_packet(const wire::Envelope& env, wire::ByteView payload) {
        auto p = std::make_shared<wire::Bytes>();
        p->reserve(wire::kEnvelopeLen + payload.size());
        env.write_to(*p);
        p->insert(p->end(), payload.begin(), payload.end());
        return p;
    }

    static Packet descriptor_packet(wire::Envelope env, const shm::Descriptor& d) {
        env.flags = static_cast<std::uint8_t>(env.flags | wire::kFlagShm);
        env.payload_len = static_cast<std::uint32_t>(shm::kDescriptorLen);
        auto p = std::make_shared<wire::Bytes>();
        p->reserve(wire::kEnvelopeLen + shm::kDescriptorLen);
        env.write_to(*p);
        d.write_to(*p);
        return p;
    }

    std::optional<shm::Descriptor> write_shared(const wire::Envelope& env, wire::ByteView payload) {
        std::string why;
        auto d = pool_->write(env.seq, payload, why);
        if (!d) {
            ++fallbacks_;
            if (!fallback_warned_) {
                fallback_warned_ = true;
                log::write("[agmen] ⚠ '" + topic_ + "': " + why +
                           ". Este mensaje (y los que no quepan) va por TCP, copiado.");
            }
        }
        return d;
    }

    /// Lo que dijo un suscriptor (RFC 0009 §3.7).
    void on_record(Outgoing& c, const shm::Record& r) {
        switch (r.op) {
            case shm::Op::Hello: {
                if (c.hello) return;
                c.hello = true;
                if (c.remote) return;   // la memoria compartida es de cada computadora: a otra ni se prueba ni se avisa
                if (!shm::enabled()) return;
                if (!pool_) pool_ = std::make_unique<shm::Pool>(source_id_);
                std::string why;
                const auto d = pool_->probe(why);
                if (!d) {
                    if (!fallback_warned_) {
                        fallback_warned_ = true;
                        log::write("[agmen] ⚠ '" + topic_ + "': " + why + ". Los mensajes van por TCP.");
                    }
                    return;
                }
                wire::Envelope env{.encoding = wire::Encoding::None,
                                   .topic_id = topic_id_,
                                   .source_id = source_id_,
                                   .t_pub = wire::now_ns(),
                                   .clock = clock::domain()};
                env.t_data = env.t_pub;
                c.control.push_back(descriptor_packet(env, *d));
                c.shm = Outgoing::Shm::Probing;
                return;
            }
            case shm::Op::ProbeOk:
                if (c.shm == Outgoing::Shm::Probing) c.shm = Outgoing::Shm::Yes;
                return;
            case shm::Op::ProbeFailed:
                c.shm = Outgoing::Shm::No;
                return;
            case shm::Op::Release: {
                const auto it = c.lent.find({r.slot, r.generation});
                if (it == c.lent.end()) return;   // repetido o equivocado: se ignora
                c.lent_bytes -= it->second;
                c.lent.erase(it);
                if (pool_) pool_->release(r.slot);
                return;
            }
        }
    }

    /// Lee los avisos del suscriptor.
    void serve_reads(Outgoing& c) {
        std::uint8_t buf[512];
        for (;;) {
            const auto n = c.stream.recv_some(buf);
            if (n < 0) {
                c.fail(pool_.get());
                return;
            }
            if (n == 0) break;
            c.inbox.insert(c.inbox.end(), buf, buf + n);
            if (static_cast<std::size_t>(n) < sizeof buf) break;
        }
        std::size_t at = 0;
        while (c.inbox.size() - at >= shm::kRecordLen) {
            shm::Record r;
            try {
                r = shm::Record::read(wire::ByteView(c.inbox.data() + at, shm::kRecordLen));
            } catch (const wire::WireError&) {
                c.fail(pool_.get());   // no es un suscriptor de Agmen
                return;
            }
            at += shm::kRecordLen;
            on_record(c, r);
        }
        c.inbox.erase(c.inbox.begin(), c.inbox.begin() + static_cast<std::ptrdiff_t>(at));
        c.step(pool_.get());
    }

    /// `msg`: lo llama un mensaje de datos; a otra computadora sin conexión, se cuenta como no salido.
    void reconnect(bool force, bool msg) {
        const auto now = std::chrono::steady_clock::now();
        for (const auto& a : want_) {
            if (const auto it = conns_.find(a); it != conns_.end() && it->second.alive()) continue;
            const auto host = host_of(a);   // AGMEN_NET_IP también es esta computadora (RFC 0011 §9.5)
            const bool remote = net_ && host != hub::kLocalhost && host != net_ip_;
            if (!force) {
                if (const auto t = last_try_.find(a); t != last_try_.end() && now - t->second < kRetryEvery) {
                    if (remote && msg) ++unreachable_;
                    continue;
                }
            }
            last_try_[a] = now;
            if (const auto it = conns_.find(a); it != conns_.end()) forget(it);
            Outgoing c;
            c.addr = a;
            if (remote) {
                // A otra computadora puede tardar o no contestar nunca: sin esperar (lo termina el hilo).
                c.remote = true;
                try {
                    bool pending = false;
                    c.stream = net::TcpStream::connect_async(net::Address::resolve(a), net_ip_, pending);
                    c.connecting = pending;
                    c.deadline = now + kRemoteConnect;
                } catch (const net::NetError& e) {
                    if (msg) ++unreachable_;
                    warn_unreachable(a, e.code().value() != 0 ? e.code().message() : std::string(e.what()));
                    continue;
                }
            } else {
                try {
                    c.stream = net::TcpStream::connect(net::Address::resolve(a), kConnectTimeout);
                } catch (const net::NetError&) {
                    continue;   // el suscriptor no está; se reintenta en un rato (o el hub lo quita)
                }
            }
            if (announcement_) {
                c.offer(Item{announcement_}, pool_.get());
                c.step(pool_.get());
            }
            conns_.emplace(a, std::move(c));
        }
    }

    void warn_unreachable(const std::string& a, const std::string& why) {
        if (unreach_warned_.insert(a).second) log::write(unreachable_text(topic_, a, why.empty() ? "no contestó" : why));
    }

    /// Una conexión a otra computadora que no se pudo terminar (con el candado).
    void remote_failed(Outgoing& c, const std::string& why) {
        if (c.next || c.current) ++unreachable_;   // lo que esperaba salir por ella
        c.fail(pool_.get());
        c.connecting = false;
        warn_unreachable(c.addr, why);
    }

    Outgoing* find(std::intptr_t handle) {
        for (auto& [_, c] : conns_) {
            if (c.alive() && c.stream.native_handle() == handle) return &c;
        }
        return nullptr;
    }

    void loop() {
        std::vector<std::uint8_t> drain(64);
        auto last_trim = std::chrono::steady_clock::now();
        while (!closed_) {
            // Se lee de todas las conexiones (los avisos del suscriptor) y se
            // escribe en las que tienen algo que se puede mandar.
            std::vector<std::intptr_t> handles{wake_.native_handle()};
            std::vector<char> want_write{0};
            bool any_write = false;
            {
                std::lock_guard lock(mu_);
                // D6 a′: una vez por segundo, los bloques sin usar 5 s se liberan.
                const auto now = std::chrono::steady_clock::now();
                if (pool_ && now - last_trim >= std::chrono::seconds(1)) {
                    last_trim = now;
                    (void)pool_->trim(now);
                }
                for (auto& [_, c] : conns_) {
                    if (c.alive() && c.connecting && now >= c.deadline) {
                        remote_failed(c, "no contestó en " + std::to_string(kRemoteConnect.count() / 1000) + " s");
                    }
                }
                for (const auto& [_, c] : conns_) {
                    if (!c.alive()) continue;
                    handles.push_back(c.stream.native_handle());
                    want_write.push_back(c.wants_write() ? 1 : 0);
                    any_write = any_write || c.wants_write();
                }
            }
            const std::size_t n_handles = handles.size();
            std::unique_ptr<bool[]> write(new bool[n_handles]());
            std::unique_ptr<bool[]> readable(new bool[n_handles]());
            std::unique_ptr<bool[]> writable(new bool[n_handles]());
            for (std::size_t i = 0; i < n_handles; ++i) write[i] = want_write[i] != 0;
            int n = 0;
            try {
                n = net::wait_rw(handles, std::span<const bool>(write.get(), n_handles),
                                 std::chrono::milliseconds(any_write ? 500 : 1000),
                                 std::span<bool>(readable.get(), n_handles), std::span<bool>(writable.get(), n_handles));
            } catch (const net::NetError&) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            if (n <= 0) continue;
            if (readable[0]) (void)wake_.recv_from(drain, std::chrono::milliseconds(0));
            std::lock_guard lock(mu_);
            // La conexión puede haberse cerrado entre la lista y ahora: se busca de nuevo.
            for (std::size_t i = 1; i < n_handles; ++i) {
                if (!readable[i] && !writable[i]) continue;
                Outgoing* c = find(handles[i]);
                if (c == nullptr) continue;
                if (c->connecting) {
                    // Windows dice que una conexión en curso falló como "legible" (POLLERR/POLLHUP).
                    if (const int err = c->stream.finish_connect(); err != 0) {
                        remote_failed(*c, err == -1 ? std::string("se cerró") : std::system_category().message(err));
                        continue;
                    }
                    c->connecting = false;
                    c->step(pool_.get());
                    continue;
                }
                if (readable[i]) serve_reads(*c);
                if (writable[i] && c->alive()) c->step(pool_.get());
            }
        }
    }
};

// ---------------------------------------------------------------------------
// TCP del lado del Subscriber
// ---------------------------------------------------------------------------

/// Lo que un suscriptor tiene de UNA conexión (un publicador): el socket, por
/// donde también le devuelve los bloques, y los bloques que ya abrió. Vive
/// mientras viva la conexión o algún préstamo: si cierras el Subscriber con
/// mensajes prestados, la conexión se cierra cuando sueltas el último (así el
/// publicador no reescribe un bloque que todavía ves).
struct Peer {
    // Los préstamos se devuelven desde cualquier hilo, y TcpStream cierra el
    // socket si falla: todo lo que toca `stream` pasa por este candado.
    std::mutex mu;
    net::TcpStream stream;
    wire::Bytes pending;        // avisos que el sistema no aceptó todavía
    bool closed = false;
    // Solo los usa el hilo que recibe: los bloques que ya abrió, para no
    // abrirlos con cada mensaje, y cuándo los nombró el último descriptor.
    struct Cached {
        std::shared_ptr<const shm::Segment> segment;
        std::chrono::steady_clock::time_point used{};
    };
    std::map<std::uint32_t, Cached> blocks;
    bool hello = false;         // le dijimos que podemos

    /// Suelta los que ningún descriptor nombró en kIdleFree (D6 a′): el
    /// publicador ya pudo haberlos liberado, y en Windows la memoria de un
    /// bloque no se libera mientras alguien lo tenga abierto.
    void forget_idle(std::chrono::steady_clock::time_point now) {
        std::erase_if(blocks, [&](const auto& kv) { return now - kv.second.used >= shm::kIdleFree; });
    }

    [[nodiscard]] std::intptr_t handle() {
        std::lock_guard lock(mu);
        return stream.native_handle();
    }

    std::ptrdiff_t recv(std::span<std::uint8_t> buffer) {
        std::lock_guard lock(mu);
        return stream.recv_some(buffer);
    }

    void say(const shm::Record& r) {
        std::lock_guard lock(mu);
        if (closed) return;
        r.write_to(pending);
        flush_locked();
    }

    void flush() {
        std::lock_guard lock(mu);
        flush_locked();
    }

    void flush_locked() {
        while (!pending.empty() && !closed) {
            const auto n = stream.send_some(pending);
            if (n < 0) {
                closed = true;
                pending.clear();
                return;
            }
            if (n == 0) return;
            pending.erase(pending.begin(), pending.begin() + n);
        }
    }

    void mark_closed() {
        std::lock_guard lock(mu);
        closed = true;
        pending.clear();
    }
};

/// Cuántos mensajes (y bytes) tiene prestados un Subscriber (D6).
struct Lent {
    std::atomic<std::size_t> count{0};
    std::atomic<std::size_t> bytes{0};

    /// ¿Se puede prestar uno de `n` bytes más? Siempre al menos uno.
    [[nodiscard]] bool room_for(std::size_t n) const {
        const auto c = count.load();
        return c == 0 || (c < shm::kMaxLent && bytes.load() + n <= shm::kMaxLentBytes);
    }
};

/// Un mensaje prestado: mientras exista, el bloque del publicador no se toca.
struct Lease {
    std::shared_ptr<Peer> peer;
    std::shared_ptr<const shm::Segment> segment;
    std::shared_ptr<Lent> lent;
    std::uint32_t slot = 0;
    std::uint64_t generation = 0;
    std::size_t length = 0;

    Lease(std::shared_ptr<Peer> p, std::shared_ptr<const shm::Segment> s, std::shared_ptr<Lent> l, std::uint32_t sl,
          std::uint64_t g, std::size_t n)
        : peer(std::move(p)), segment(std::move(s)), lent(std::move(l)), slot(sl), generation(g), length(n) {
        lent->count.fetch_add(1);
        lent->bytes.fetch_add(length);
    }
    Lease(const Lease&) = delete;
    Lease& operator=(const Lease&) = delete;
    ~Lease() {
        lent->count.fetch_sub(1);
        lent->bytes.fetch_sub(length);
        try {
            peer->say(shm::Record{shm::Op::Release, slot, generation});
        } catch (...) {
            // sin memoria para el aviso: el bloque vuelve al cerrarse la conexión
        }
    }
};

/// Un paquete completo que llegó por TCP, y de qué conexión.
struct Arrived {
    wire::Bytes packet;
    std::string from;
    std::shared_ptr<Peer> peer;
};

/// Una conexión aceptada: junta bytes hasta tener un paquete completo.
struct Incoming {
    std::shared_ptr<Peer> peer = std::make_shared<Peer>();
    std::string from;
    wire::Bytes head;        // los primeros 64 bytes
    wire::Bytes packet;      // el paquete completo en construcción
    std::size_t filled = 0;  // cuánto de packet ya llegó (0 = todavía se junta head)
    std::vector<std::uint8_t> chunk = std::vector<std::uint8_t>(kReadChunk);

    [[nodiscard]] std::intptr_t handle() const { return peer->handle(); }

    /// Lee lo que haya; deja en `out` los paquetes completos. false si se cerró.
    bool read(std::deque<Arrived>& out) {
        if (!packet.empty()) {
            // Ya sabemos cuánto mide: se lee directo en su lugar.
            const auto n = peer->recv(std::span<std::uint8_t>(packet.data() + filled, packet.size() - filled));
            if (n < 0) return false;
            filled += static_cast<std::size_t>(n);
            if (filled == packet.size()) {
                out.push_back(Arrived{std::move(packet), from, peer});
                packet.clear();
                filled = 0;
            }
            return true;
        }
        const auto n = peer->recv(chunk);
        if (n < 0) return false;
        feed(wire::ByteView(chunk.data(), static_cast<std::size_t>(n)), out);
        return true;
    }

    void feed(wire::ByteView data, std::deque<Arrived>& out) {
        while (!data.empty()) {
            if (packet.empty()) {
                const std::size_t need = wire::kEnvelopeLen - head.size();
                const std::size_t take = std::min(need, data.size());
                head.insert(head.end(), data.begin(), data.begin() + static_cast<std::ptrdiff_t>(take));
                data = data.subspan(take);
                if (head.size() < wire::kEnvelopeLen) return;
                if (std::memcmp(head.data(), "AGM", 3) != 0) {
                    throw wire::WireError(wire::WireError::Kind::BadMagic,
                                          "magic inválido en la conexión de " + from +
                                              ": el paquete no viene de un nodo de Agmen");
                }
                std::uint16_t header_len = 0;
                std::uint32_t payload_len = 0;
                std::memcpy(&header_len, head.data() + 4, 2);
                std::memcpy(&payload_len, head.data() + 56, 4);
                const std::size_t total = std::size_t{header_len} + payload_len;
                if (total > wire::max_message() + wire::kEnvelopeLen) {
                    throw wire::WireError(wire::WireError::Kind::TooBig, wire::too_big_text(from, total));
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
                out.push_back(Arrived{std::move(packet), from, peer});
                packet.clear();
                filled = 0;
            }
        }
    }
};

struct Listener {
    net::TcpListener listener;
    bool shm = false;   // ¿decirle a cada publicador que podemos memoria compartida?
    std::vector<std::unique_ptr<Incoming>> conns;
    std::deque<Arrived> ready;
    // Modo red (RFC 0011 §2): los puertos de escucha anteriores (siguen aceptando hasta retire_old()) y
    // de qué IP se aceptan conexiones (sin valor = de todas: escucha solo en 127.0.0.1, lo de siempre).
    std::vector<net::TcpListener> old;
    std::optional<std::vector<std::string>> allow;
    std::function<void(const std::string&)> on_reject;
    std::string net_ip;   // AGMEN_NET_IP: también es esta computadora (memoria compartida, RFC 0011 §9.5)

    Listener(std::string_view bind, bool can_shm) : listener(net::TcpListener::bind(bind)), shm(can_shm) {}

    void handles(std::vector<std::intptr_t>& out) const {
        out.push_back(listener.native_handle());
        for (const auto& o : old) out.push_back(o.native_handle());
        for (const auto& c : conns) {
            if (const auto h = c->handle(); h != -1) out.push_back(h);   // una que ya se cerró, no
        }
    }

    /// Un puerto de escucha nuevo en `bind`; el de antes sigue aceptando hasta retire_old(). Las
    /// conexiones ya aceptadas no se tocan. Si no se puede abrir, lanza NetError y nada cambia.
    void rebind(std::string_view bind) {
        auto fresh = net::TcpListener::bind(bind);
        old.push_back(std::move(listener));
        listener = std::move(fresh);
    }

    /// Cierra los puertos de escucha anteriores (no las conexiones que ya aceptaron).
    void retire_old() { old.clear(); }

    /// De qué IP se aceptan conexiones; las aceptadas de una IP que ya no está se cierran.
    void set_allow(std::vector<std::string> ips, std::function<void(const std::string&)> reject) {
        allow = std::move(ips);
        on_reject = std::move(reject);
        for (auto it = conns.begin(); it != conns.end();) {
            if (ip_in((*it)->from, *allow)) {
                ++it;
                continue;
            }
            (*it)->peer->mark_closed();
            (*it)->peer->blocks.clear();
            it = conns.erase(it);
        }
    }

    void accept_from(net::TcpListener& l) {
        while (auto s = l.accept()) {
            const auto ip = host_of(s->peer());
            if (allow && !ip_in(ip, *allow)) {
                s->close();   // de una computadora que no es de [[machine]]
                if (on_reject) on_reject(ip);
                continue;
            }
            auto c = std::make_unique<Incoming>();
            c->from = s->peer();
            c->peer->stream = std::move(*s);
            // En modo red, la memoria compartida solo con esta computadora (en local, lo de siempre).
            if (shm && (!allow || ip == hub::kLocalhost || (!net_ip.empty() && ip == net_ip))) {
                // El suscriptor habla primero (RFC 0009 §3.3): un publicador de
                // 0.10 nunca lee esto, y uno de 0.11 sabe que puede probar.
                c->peer->hello = true;
                c->peer->say(shm::Record{shm::Op::Hello, 0, 0});
            }
            conns.push_back(std::move(c));
        }
    }

    /// Atiende los sockets listos (handles[from..], como los dio handles()): acepta y lee.
    void service(const std::vector<std::intptr_t>& handles, const bool* is_ready, std::size_t from) {
        for (std::size_t i = from; i < handles.size(); ++i) {
            if (!is_ready[i]) continue;
            if (handles[i] == listener.native_handle()) {
                accept_from(listener);
                continue;
            }
            if (const auto o = std::find_if(old.begin(), old.end(),
                                            [&](const auto& l) { return l.native_handle() == handles[i]; });
                o != old.end()) {
                accept_from(*o);
                continue;
            }
            const auto it = std::find_if(conns.begin(), conns.end(),
                                         [&](const auto& c) { return c->handle() == handles[i]; });
            if (it == conns.end()) continue;
            bool alive = true;
            try {
                alive = (*it)->read(ready);
            } catch (...) {
                drop(it);
                throw;
            }
            if (alive) {
                (*it)->peer->flush();   // avisos que no habían cabido
            } else {
                drop(it);
            }
        }
        // Una conexión que se cerró al devolver un bloque (el publicador se fue).
        conns.erase(std::remove_if(conns.begin(), conns.end(),
                                   [](const auto& c) {
                                       if (c->handle() != -1) return false;
                                       c->peer->mark_closed();
                                       c->peer->blocks.clear();   // los prestados siguen vivos en su Lease
                                       return true;
                                   }),
                    conns.end());
    }

    void drop(std::vector<std::unique_ptr<Incoming>>::iterator it) {
        (*it)->peer->mark_closed();
        (*it)->peer->blocks.clear();
        conns.erase(it);
    }

    void forget_idle(std::chrono::steady_clock::time_point now) {
        for (auto& c : conns) c->peer->forget_idle(now);
    }

    [[nodiscard]] std::size_t mapped() const {
        std::size_t n = 0;
        for (const auto& c : conns) n += c->peer->blocks.size();
        return n;
    }
};

}  // namespace

// ---------------------------------------------------------------------------
// PublisherCore
// ---------------------------------------------------------------------------

struct PublisherCore::State {
    TypeInfo type;
    std::mutex mu;   // se puede publicar desde varios grupos de un Node (hilos)
    std::string net_ip;   // AGMEN_NET_IP (solo pruebas, RFC 0011 §3); "" = no está. Antes que el socket
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
    // Modo red (RFC 0011 §3): los destinos que no son 127.0.0.1 salen por otro socket (net_socket).
    std::optional<net::UdpSocket> net_socket;   // se abre la primera vez que hay un destino de fuera
    std::vector<char> remote;                   // remote[i]: dests[i] es de otra computadora
    bool any_remote = false;                    // false = todos locales: lo de siempre, sin trabajo de más
    std::atomic<std::uint64_t> unreachable{0};
    std::set<std::string> unreach_warned;
    std::atomic<std::uint64_t> rejected{0};   // control que no vino del hub (RFC 0011 §9.1)

    State(const std::string& to, TypeInfo t, std::string label)
        // Por topic todo es local (nivel 2): 127.0.0.1, así Windows no pregunta por el firewall.
        : type(std::move(t)),
          net_ip(hub::is_topic(to) ? net_ip_from_env() : std::string()),   // antes de abrir nada
          socket(net::UdpSocket::bind(hub::is_topic(to) ? "127.0.0.1:0" : "0.0.0.0:0")),
          topic(hub::is_topic(to) ? to : (label.empty() ? "/" + type.name : std::move(label))),
          source_id(wire::random_source_id()) {
        topic_id = wire::topic_id(topic);
        if (type.arrow) fanout = std::make_unique<Fanout>(topic, source_id, topic_id, hub::is_topic(to), net_ip);
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
            fanout->send_packet(std::make_shared<const wire::Bytes>(ann));
        } else {
            for (std::size_t i = 0; i < dests.size(); ++i) (void)send_any(ann, i);
        }
        announced = true;
        last_announce = std::chrono::steady_clock::now();
    }

    /// Lee las respuestas del hub (a quién mandarle los datos).
    void drain(std::chrono::milliseconds wait) {
        while (const auto rx = socket.recv_from(buf, wait)) {
            wait = std::chrono::milliseconds(0);
            const wire::ByteView data(buf.data(), rx->size);
            if (hub::is_control(data) && !link->from_hub(rx->from)) {   // RFC 0011 §9.1: solo el hub
                rejected.fetch_add(1);
                link->warn_foreign(topic, rx->from);
                continue;
            }
            std::optional<std::vector<std::string>> subs;
            std::optional<std::vector<std::string>> tcp;
            link->handle(data, subs, &tcp);
            if (subs && fanout) {
                tcp_dests = tcp ? *tcp : std::vector<std::string>{};
                fanout->update(tcp_dests, std::make_shared<const wire::Bytes>(announcement()));
            } else if (subs) {
                std::vector<net::Address> fresh;
                std::vector<char> fresh_remote;
                fresh.reserve(subs->size());
                fresh_remote.reserve(subs->size());
                for (const auto& a : *subs) {
                    fresh.push_back(net::Address::resolve(a));
                    // AGMEN_NET_IP también es esta computadora (RFC 0011 §9.5).
                    const auto host = host_of(a);
                    fresh_remote.push_back(static_cast<char>(host != hub::kLocalhost && host != net_ip ? 1 : 0));
                }
                const bool fresh_any =
                    std::find(fresh_remote.begin(), fresh_remote.end(), char{1}) != fresh_remote.end();
                if (fresh_any) {
                    open_net_socket();
                } else {
                    net_socket.reset();   // sin destinos de otra computadora: ningún puerto abierto a la red (§9.3)
                }
                const auto ann = announcement();
                std::vector<char> known(fresh.size(), 0);
                for (std::size_t i = 0; i < fresh.size(); ++i) {
                    for (const auto& d : dests) known[i] = static_cast<char>(known[i] || d == fresh[i]);
                }
                dests = std::move(fresh);
                remote = std::move(fresh_remote);
                any_remote = fresh_any;
                for (std::size_t i = 0; i < dests.size(); ++i) {
                    if (!known[i]) (void)send_any(ann, i);   // recién llegado: por el mismo camino que sus datos
                }
            }
        }
    }

    /// La primera vez que hay un destino de otra computadora (con `mu`).
    void open_net_socket() {
        if (net_socket) return;
        const std::string bind = (net_ip.empty() ? std::string("0.0.0.0") : net_ip) + ":0";
        try {
            net_socket.emplace(net::UdpSocket::bind(bind));   // AGMEN_NET_IP: los envíos salen desde esa IP
        } catch (const net::NetError& e) {
            log::write("[agmen] ⚠ '" + topic + "': no pude abrir el socket para mandar a otra computadora: " +
                       e.what() + ". Lo que vaya a otra computadora no sale (se cuenta en unreachable).");
        }
    }

    /// A un destino de otra computadora: por el socket que no está atado a 127.0.0.1. Si no sale, no
    /// frena a los demás: se cuenta y se avisa una vez por destino.
    bool send_net(wire::ByteView data, const net::Address& to) {
        std::string why;
        if (net_socket) {
            try {
                return net_socket->send_to(data, to) == net::SendStatus::Ok;
            } catch (const net::NetError& e) {
                why = e.code().value() != 0 ? e.code().message() : std::string(e.what());
            }
        } else {
            why = "no pude abrir el socket para mandar a otra computadora";
        }
        ++unreachable;
        const auto addr = to.to_string();
        if (unreach_warned.insert(addr).second) log::write(unreachable_text(topic, addr, why));
        return false;
    }

    bool send_any(wire::ByteView data, std::size_t i) {
        if (any_remote && remote[i] != 0) return send_net(data, dests[i]);
        return socket.send_to(data, dests[i]) == net::SendStatus::Ok;
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
    const wire::Envelope env{.encoding = s.type.arrow ? wire::Encoding::Arrow : wire::Encoding::Compact,
                             .schema_id = s.type.schema_id,
                             .topic_id = s.topic_id,
                             .source_id = s.source_id,
                             .seq = s.seq,
                             .t_data = t_data,
                             .t_pub = wire::now_ns(),
                             .payload_len = static_cast<std::uint32_t>(payload.size()),
                             .clock = clock::domain()};
    ++s.seq;
    if (s.fanout) return s.fanout->send(env, payload) ? net::SendStatus::Ok : net::SendStatus::NoListener;
    wire::Bytes packet;
    packet.reserve(wire::kEnvelopeLen + payload.size());
    env.write_to(packet);
    packet.insert(packet.end(), payload.begin(), payload.end());
    auto status = net::SendStatus::NoListener;
    if (!s.any_remote) {   // todos en esta computadora: lo de siempre, sin trabajo de más
        for (const auto& d : s.dests) {
            if (s.socket.send_to(packet, d) == net::SendStatus::Ok) status = net::SendStatus::Ok;
        }
    } else {
        for (std::size_t i = 0; i < s.dests.size(); ++i) {
            if (s.send_any(packet, i)) status = net::SendStatus::Ok;
        }
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
std::size_t PublisherCore::shm_subscribers() const { return s_->fanout ? s_->fanout->shm_subscribers() : 0; }
std::uint64_t PublisherCore::shm_fallbacks() const { return s_->fanout ? s_->fanout->shm_fallbacks() : 0; }
std::size_t PublisherCore::shm_bytes() const { return s_->fanout ? s_->fanout->shm_bytes() : 0; }
std::uint64_t PublisherCore::unreachable() const {
    return s_->unreachable.load() + (s_->fanout ? s_->fanout->unreachable() : 0);
}
std::uint64_t PublisherCore::rejected() const { return s_->rejected.load(); }

// ---------------------------------------------------------------------------
// SubscriberCore
// ---------------------------------------------------------------------------

struct SubscriberCore::State {
    TypeInfo type;
    std::string where;
    std::string net_ip;   // AGMEN_NET_IP (solo pruebas, RFC 0011 §2); "" = no está. Antes que el socket
    net::UdpSocket socket;
    std::unique_ptr<Listener> tcp;   // solo tipos arrow
    std::vector<std::uint8_t> buf = std::vector<std::uint8_t>(65536);
    std::optional<hub::Link> link;
    std::string topic;
    std::map<std::uint64_t, wire::Announcement> announced;
    std::string last_from;
    bool clock_warned = false;
    std::mutex link_mu;   // el enlace con el hub (heartbeat() puede venir de otro hilo)
    // Memoria compartida (RFC 0009)
    std::shared_ptr<Lent> lent = std::make_shared<Lent>();
    bool tcp_warned = false;
    bool lent_warned = false;
    std::chrono::steady_clock::time_point last_forget = std::chrono::steady_clock::now();
    // Modo red (RFC 0011 §2): solo por topic, y solo cuando el hub lo dice (un `ok` con peers).
    std::vector<std::pair<std::string, std::string>> fields;   // los del latido (para cambiar el puerto)
    std::string listen_ip;
    bool net = false;                                   // ya escucha en la red
    bool net_failed = false;
    std::optional<std::vector<std::string>> allowed;    // de qué IP acepta; sin valor = sin filtro (local)
    std::optional<net::UdpSocket> old_udp;              // el socket de antes, mientras se cambia
    std::optional<std::chrono::steady_clock::time_point> old_until;   // hasta cuándo se lee (kNetOld tras confirmar)
    std::uint64_t moves = 0;                            // cuántas veces cambiaron los sockets
    std::atomic<std::uint64_t> rejected{0};
    std::set<std::string> rejected_ips;
    std::chrono::steady_clock::time_point last_ok{};   // el último `ok` del hub (modo red, RFC 0011 §9.2)
    bool expired = false;                              // ya volvió a solo lo local por falta de `ok` (avisado)

    void forget_idle_blocks(bool now_please) {
        const auto now = std::chrono::steady_clock::now();
        if (!tcp || (!now_please && now - last_forget < std::chrono::seconds(1))) return;
        last_forget = now;
        tcp->forget_idle(now);
    }

    State(const std::string& w, TypeInfo t)
        : type(std::move(t)),
          where(w),
          net_ip(hub::is_topic(w) ? net_ip_from_env() : std::string()),   // antes de abrir nada: error claro
          socket(net::UdpSocket::bind(hub::is_topic(w) ? "127.0.0.1:0" : w)),
          listen_ip(hub::is_topic(w) ? std::string(hub::kLocalhost) : host_of(w)) {
        if (type.arrow) {
            // Los mensajes arrow llegan por TCP: con dirección fija, la misma IP y el mismo puerto.
            tcp = hub::is_topic(w) ? std::make_unique<Listener>("127.0.0.1:0", shm::enabled()) : same_port_listener(w);
            tcp->net_ip = net_ip;
        }
        if (hub::is_topic(w)) {
            check_topic_or_throw(w);
            topic = w;
            const auto id = hub::hex16(wire::random_source_id());
            std::vector<std::pair<std::string, std::string>> beat{{"id", id},
                                                                    {"node", hub::node_name()},
                                                                    {"pid", hub::process_id()},
                                                                    {"topic", w},
                                                                    {"schema", hub::hex16(type.schema_id)},
                                                                    {"type", type.name},
                                                                    {"port", std::to_string(socket.local_port())}};
            if (tcp) beat.emplace_back("tcp", std::to_string(tcp->listener.local_port()));
            link.emplace(id, hub::line("sub", beat));
            link->beat(socket);
            fields = std::move(beat);
        }
    }

    // --- modo red (RFC 0011 §2): lo mismo que _on_ok, _go_net, _set_allowed, _reject y _retire_old
    // de python/agmen/pubsub.py ---

    /// Un `ok` del hub (con `link_mu`): con `peers`, el hub está en modo red. `via`: el socket por
    /// donde llegó.
    void on_ok(std::intptr_t via) {
        const auto peers = link->peers();
        last_ok = std::chrono::steady_clock::now();
        if (peers && !peers->empty()) expired = false;
        if (peers && !net) go_net();
        if (!net) return;
        if (via == socket.native_handle() && old_udp && !old_until) {
            old_until = std::chrono::steady_clock::now() + kNetOld;   // el hub confirmó el registro nuevo
        }
        // Sin peers (el hub volvió a modo local): los mismos sockets, pero solo lo local.
        set_allowed(peers ? *peers : std::vector<std::string>{});
    }

    /// Sockets nuevos en todas las interfaces (o en AGMEN_NET_IP); se registra con ellos y net=1.
    /// Los de antes se siguen leyendo hasta kNetOld después de que el hub confirme.
    void go_net() {
        const std::string ip = net_ip.empty() ? std::string("0.0.0.0") : net_ip;
        try {
            auto udp = net::UdpSocket::bind(ip + ":0");
            if (tcp) tcp->rebind(ip + ":0");   // si falla, el Listener queda como estaba
            old_udp.emplace(std::move(socket));
            socket = std::move(udp);
        } catch (const net::NetError& e) {
            if (!net_failed) {
                net_failed = true;
                log::write("[agmen] ⚠ '" + topic + "': el hub está en modo red pero no pude escuchar en " + ip +
                           ": " + e.what() +
                           ". Sigo solo en esta computadora (127.0.0.1); lo vuelvo a intentar con cada latido.");
            }
            return;
        }
        net = true;
        ++moves;
        listen_ip = ip;
        bool has_net = false;
        for (auto& [k, v] : fields) {
            if (k == "port") v = std::to_string(socket.local_port());
            if (k == "tcp" && tcp) v = std::to_string(tcp->listener.local_port());
            has_net = has_net || k == "net";
        }
        if (!has_net) fields.emplace_back("net", "1");
        link->set_beat(hub::line("sub", fields));
        link->beat(socket);
    }

    void set_allowed(const std::vector<std::string>& ips) {
        std::vector<std::string> fresh{std::string(hub::kLocalhost)};
        if (!net_ip.empty()) fresh.push_back(net_ip);
        fresh.insert(fresh.end(), ips.begin(), ips.end());
        if (allowed && *allowed == fresh) return;
        allowed = fresh;
        if (tcp) tcp->set_allow(std::move(fresh), [this](const std::string& ip) { reject(ip); });
    }

    /// Ningún `ok` del hub en kNetExpire: como un `ok` sin peers, solo lo local (RFC 0011 §9.2). Solo se
    /// llama con la lista de IP ya armada (en modo red).
    void expire_check() {
        if (std::chrono::steady_clock::now() - last_ok <= hub::kNetExpire) return;
        const std::size_t n_local = net_ip.empty() ? 1 : 2;
        if (allowed && allowed->size() == n_local) return;   // ya solo lo local
        set_allowed({});
        if (!expired) {
            expired = true;
            log::write("[agmen] ⚠ '" + topic + "': el hub no contesta hace más de " +
                       std::to_string(hub::kNetExpire.count() / 1000) +
                       " s; dejo de aceptar datos de otras computadoras hasta que vuelva (¿se cerró 'agmen hub' o "
                       "'agmen run'?)");
        }
    }

    void reject(const std::string& ip) {
        rejected.fetch_add(1);
        // Una vez por IP; con un tope, para que una avalancha de IP distintas no llene la salida.
        if (rejected_ips.size() < kMaxRejectedWarned && rejected_ips.insert(ip).second) {
            log::write("[agmen] '" + topic + "': llegó un dato de " + ip +
                       ", que no es una computadora de [[machine]]: lo descarté (¿otra red, otro robot?)");
        }
    }

    /// Cierra los sockets de antes (las conexiones TCP ya aceptadas siguen).
    void retire_old() {
        old_udp.reset();
        old_until.reset();
        if (tcp) tcp->retire_old();
    }

    ~State() {
        if (link && socket.valid()) link->bye(socket);
    }

    // El TCP en la misma IP y el mismo puerto que el UDP. Con el puerto 0, el que
    // eligió el sistema para UDP puede no servir en TCP: ocupado (la conexión de
    // otro programa) o, en Windows, reservado (Hyper-V, WSL y Docker apartan rangos
    // enteros de puertos TCP: WSAEACCES). Como Windows da los puertos de corrido,
    // probar con otro UDP caía en el mismo rango: se pide primero el TCP (el
    // sistema nunca da uno reservado) y el UDP se abre en ese número. Ese número
    // puede no servir en UDP (un socket UDP de otro programa, o un rango que
    // Windows apartó solo para UDP) y, como los TCP también salen de corrido, el
    // siguiente intento cae en el número siguiente del mismo bloque: se sigue
    // hasta cruzarlo (igual que _same_port_tcp en Python).
    static constexpr int kSamePortTries = 2000;
    std::unique_ptr<Listener> same_port_listener(const std::string& w) {
        const bool any_port = w.size() >= 2 && w.compare(w.size() - 2, 2, ":0") == 0;
        try {
            return std::make_unique<Listener>(host_of(w) + ":" + std::to_string(socket.local_port()),
                                              shm::enabled());
        } catch (const net::NetError&) {
            if (!any_port) throw;
        }
        for (int attempt = 1;; ++attempt) {
            auto tcp_first = std::make_unique<Listener>(host_of(w) + ":0", shm::enabled());
            try {
                socket = net::UdpSocket::bind(host_of(w) + ":" + std::to_string(tcp_first->listener.local_port()));
                return tcp_first;
            } catch (const net::NetError& e) {
                if (attempt >= kSamePortTries) {
                    throw net::NetError(e.code().value(),
                                        "no encontré un puerto libre a la vez en UDP y en TCP en " + host_of(w) +
                                            " tras " + std::to_string(kSamePortTries) + " intentos (el último UDP: " +
                                            e.what() +
                                            "). Algo tiene tomados o apartados muchos puertos UDP seguidos; en "
                                            "Windows, revísalo con 'netsh int ipv4 show excludedportrange "
                                            "protocol=udp'. O usa una dirección con puerto fijo (ej. 127.0.0.1:7500).");
                }
            }
        }
    }

    /// RFC 0001 §8.8: un mensaje de otro reloj (real en un robot simulado, o al
    /// revés) se avisa una vez. No es un error: el dato llega igual.
    void warn_clock(const wire::Envelope& env) {
        if (clock_warned || env.clock == clock::domain()) return;
        clock_warned = true;
        log::write("[agmen] ⚠ '" + where + "' llega con reloj " + clock_name(env.clock) +
                   " y este nodo usa reloj " + clock_name(clock::domain()) +
                   ". ¿Corriste el nodo fuera de 'agmen run'? Usa el mismo reloj: AGMEN_CLOCK=" +
                   (env.clock == wire::Clock::Sim ? "sim" : env.clock == wire::Clock::Replay ? "replay" : "real"));
    }

    /// RFC 0009 §3.3: no se pudo abrir la memoria del publicador. Llega por TCP
    /// igual; se dice una vez por qué.
    void warn_tcp(const std::string& why) {
        if (tcp_warned) return;
        tcp_warned = true;
        log::write("[agmen] '" + where + "' llega por TCP y no por memoria compartida: " + why +
                   ". ¿El publicador está en otra computadora o en otro contenedor?");
    }

    /// Lanza WireError si el mensaje no es del tipo que se espera.
    void check_type(const wire::Envelope& env) {
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
    }

    /// Un paquete de datos (de UDP o de TCP) → la muestra, o nullopt si era un
    /// anuncio. Lanza WireError si no es válido o es de otro tipo. `peer` es la
    /// conexión TCP de donde vino (nullptr = UDP).
    std::optional<RawSample> handle_packet(wire::ByteView data, std::string from,
                                           const std::shared_ptr<Peer>& peer = nullptr) {
        last_from = from;
        const auto [env, payload] = wire::Envelope::read_from(data, peer != nullptr && peer->hello);
        if (env.encoding == wire::Encoding::Announcement) {
            announced[env.schema_id] = wire::read_announcement(env, payload);
            return std::nullopt;
        }
        if ((env.flags & wire::kFlagShm) != 0) return from_shared(env, payload, std::move(from), peer);
        check_type(env);
        if (!type.arrow && type.payload_size != 0 && payload.size() != type.payload_size) {
            throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                                  wire::payload_size_text(type.name, type.payload_size, payload.size()));
        }
        warn_clock(env);
        return RawSample{env, wire::Bytes(payload.begin(), payload.end()), std::move(from)};
    }

    /// Un descriptor de memoria compartida (RFC 0009 §3.3, §3.4).
    std::optional<RawSample> from_shared(wire::Envelope env, wire::ByteView payload, std::string from,
                                         const std::shared_ptr<Peer>& peer) {
        const auto d = shm::Descriptor::read(payload);
        if (env.encoding == wire::Encoding::None) {
            // La prueba: ¿puedo abrir la memoria de este publicador?
            try {
                const auto seg = shm::Segment::open(d.name, d.size);
                std::atomic_thread_fence(std::memory_order_acquire);
                if (shm::BlockHeader::read(seg.data()).owner != env.source_id) {
                    throw shm::ShmError("el bloque " + d.name + " es de otro publicador");
                }
                peer->say(shm::Record{shm::Op::ProbeOk, d.slot, d.generation});
            } catch (const std::exception& e) {
                peer->say(shm::Record{shm::Op::ProbeFailed, d.slot, d.generation});
                warn_tcp(e.what());
            }
            return std::nullopt;
        }
        // El bloque está apartado para nosotros: si algo falla, se devuelve.
        try {
            check_type(env);
        } catch (...) {
            peer->say(shm::Record{shm::Op::Release, d.slot, d.generation});
            throw;
        }
        std::shared_ptr<const shm::Segment> seg;
        try {
            auto& cached = peer->blocks[d.slot];
            if (!cached.segment || cached.segment->name() != d.name || cached.segment->size() != d.size) {
                cached.segment = std::make_shared<const shm::Segment>(shm::Segment::open(d.name, d.size));
            }
            cached.used = std::chrono::steady_clock::now();
            seg = cached.segment;
            std::atomic_thread_fence(std::memory_order_acquire);
            const auto h = shm::BlockHeader::read(seg->data());
            if (h.owner != env.source_id || h.generation != d.generation || h.length != d.length) {
                throw shm::ShmError("el bloque " + d.name + " no guarda el mensaje que dice el descriptor");
            }
        } catch (const shm::ShmError& e) {
            peer->blocks.erase(d.slot);
            peer->say(shm::Record{shm::Op::Release, d.slot, d.generation});
            peer->say(shm::Record{shm::Op::ProbeFailed, d.slot, d.generation});   // lo que siga, por TCP
            warn_tcp(e.what());
            return std::nullopt;
        }
        env.payload_len = static_cast<std::uint32_t>(d.length);
        warn_clock(env);
        const wire::ByteView view(seg->data() + d.offset, static_cast<std::size_t>(d.length));
        if (!lent->room_for(view.size())) {
            // Ya tienes muchos prestados: este se copia y el bloque vuelve en el acto (D6).
            if (!lent_warned) {
                lent_warned = true;
                log::write("[agmen] '" + where + "': tienes " + std::to_string(lent->count.load()) +
                           " mensajes de memoria compartida sin soltar (el tope es " + std::to_string(shm::kMaxLent) +
                           " o 64 MiB); los siguientes se copian");
            }
            wire::Bytes copy(view.begin(), view.end());
            peer->say(shm::Record{shm::Op::Release, d.slot, d.generation});
            return RawSample{env, std::move(copy), std::move(from)};
        }
        auto lease = std::make_shared<Lease>(peer, std::move(seg), lent, d.slot, d.generation, view.size());
        return RawSample{env, Payload(view, std::move(lease)), std::move(from)};
    }

    /// Un datagrama UDP: control del hub, o datos. `via`: el socket por donde llegó.
    std::optional<RawSample> handle_datagram(wire::ByteView data, std::string from, std::intptr_t via) {
        if (allowed) {   // sin valor hasta que escucha en la red: sin trabajo de más
            expire_check();
            if (!ip_in(from, *allowed)) {
                reject(host_of(from));
                return std::nullopt;
            }
        }
        if (link && hub::is_control(data) && !link->from_hub(from)) {   // RFC 0011 §9.1: solo si es del hub
            rejected.fetch_add(1);
            std::lock_guard lock(link_mu);
            link->warn_foreign(topic, from);
            return std::nullopt;
        }
        if (link) {
            std::optional<std::vector<std::string>> unused;
            std::unique_lock lock(link_mu);
            const auto oks = link->oks();
            if (link->handle(data, unused)) {
                if (link->oks() != oks) on_ok(via);
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
        s.forget_idle_blocks(false);
        auto now = steady_clock::now();
        const auto left = duration_cast<milliseconds>(deadline - now);
        auto wait = left.count() > 0 ? left : milliseconds(0);
        if (!s.tcp && !s.old_udp) {   // un solo socket UDP: lo de siempre
            const auto rx = s.socket.recv_from(s.buf, wait);
            if (!rx) return std::nullopt;
            if (auto out = s.handle_datagram(wire::ByteView(s.buf.data(), rx->size), rx->from,
                                             s.socket.native_handle())) {
                return out;
            }
            continue;   // control o anuncio: si ya pasó el tiempo, la siguiente lectura no espera
        }
        // Tipo arrow (UDP para control y anuncios, TCP para los datos), o el socket de antes mientras
        // se pasa a la red (RFC 0011 §2): todos a la vez.
        if (s.tcp && !s.tcp->ready.empty()) {
            auto got = std::move(s.tcp->ready.front());
            s.tcp->ready.pop_front();
            if (auto out = s.handle_packet(got.packet, std::move(got.from), got.peer)) return out;
            continue;
        }
        if (s.allowed) s.expire_check();   // antes de aceptar conexiones TCP (RFC 0011 §9.2)
        bool hasta_el_final = true;   // ¿esta espera llega hasta `deadline`? (o hasta cerrar los sockets de antes)
        if (s.old_until) {
            if (now >= *s.old_until) {
                s.retire_old();
                continue;
            }
            if (const auto until_old = duration_cast<milliseconds>(*s.old_until - now) + milliseconds(1);
                until_old < wait) {
                wait = until_old;
                hasta_el_final = false;
            }
        }
        std::vector<std::intptr_t> handles{s.socket.native_handle()};
        if (s.old_udp) handles.push_back(s.old_udp->native_handle());
        const std::size_t n_udp = handles.size();
        if (s.tcp) s.tcp->handles(handles);
        std::unique_ptr<bool[]> ready(new bool[handles.size()]());
        const int n = net::wait_readable(handles, wait, std::span<bool>(ready.get(), handles.size()));
        if (n <= 0) {
            if (hasta_el_final) return std::nullopt;
            continue;
        }
        const auto moves = s.moves;
        bool again = false;
        for (std::size_t i = 0; i < n_udp && !again; ++i) {
            if (!ready[i]) continue;
            net::UdpSocket& udp = i == 0 ? s.socket : *s.old_udp;
            if (const auto rx = udp.recv_from(s.buf, milliseconds(0))) {
                if (auto out = s.handle_datagram(wire::ByteView(s.buf.data(), rx->size), rx->from, handles[i])) {
                    return out;
                }
            }
            again = s.moves != moves;   // recién se cambiaron los sockets: se vuelve a empezar
        }
        if (again) continue;
        if (s.tcp) s.tcp->service(handles, ready.get(), n_udp);
    }
}

void SubscriberCore::heartbeat() {
    if (!s_->link) return;
    std::lock_guard lock(s_->link_mu);
    s_->link->maybe_beat(s_->socket);
}

void SubscriberCore::forget_idle_blocks() { s_->forget_idle_blocks(true); }

std::size_t SubscriberCore::shm_mapped() const { return s_->tcp ? s_->tcp->mapped() : 0; }

std::uint16_t SubscriberCore::local_port() const { return s_->socket.local_port(); }
std::uint16_t SubscriberCore::tcp_port() const { return s_->tcp ? s_->tcp->listener.local_port() : 0; }
std::string SubscriberCore::listen_ip() const { return s_->listen_ip; }
std::uint64_t SubscriberCore::rejected() const { return s_->rejected.load(); }

const std::string& SubscriberCore::last_from() const { return s_->last_from; }
std::intptr_t SubscriberCore::native_handle() const { return s_->socket.native_handle(); }
std::vector<std::intptr_t> SubscriberCore::native_handles() const {
    std::vector<std::intptr_t> out{s_->socket.native_handle()};
    if (s_->old_udp) out.push_back(s_->old_udp->native_handle());
    if (s_->tcp) s_->tcp->handles(out);
    return out;
}
const std::string& SubscriberCore::where() const { return s_->where; }
const TypeInfo& SubscriberCore::type() const { return s_->type; }
const std::map<std::uint64_t, wire::Announcement>& SubscriberCore::announced() const { return s_->announced; }

}  // namespace agmen
