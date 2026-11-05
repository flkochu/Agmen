#include "agmen/pubsub.hpp"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <deque>
#include <thread>

#include "agmen/clock.hpp"
#include "agmen/log.hpp"
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

    [[nodiscard]] bool alive() const { return stream.valid(); }
    [[nodiscard]] bool busy() const { return current || next || !control.empty(); }
    /// Un descriptor sale solo si el suscriptor no tiene ya demasiados sin
    /// devolver (8 y 128 MiB; siempre al menos dos, D6).
    [[nodiscard]] bool can_send(const Item& it) const {
        return it.slot < 0 || lent.size() < 2 ||
               (lent.size() < shm::kMaxUnreturned && lent_bytes + it.length <= shm::kMaxUnreturnedBytes);
    }
    [[nodiscard]] bool wants_write() const { return current || !control.empty() || (next && can_send(next)); }

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
        while (stream.valid()) {
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
    Fanout(std::string topic, std::uint64_t source_id, std::uint64_t topic_id)
        : topic_(std::move(topic)),
          source_id_(source_id),
          topic_id_(topic_id),
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
            reconnect(true);
        }
        wake();
    }

    /// Deja un paquete ya armado (un anuncio) en cada conexión.
    bool send_packet(Packet packet) {
        bool ok = false;
        bool wake_up = false;
        {
            std::lock_guard lock(mu_);
            reconnect(false);
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
            reconnect(false);
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

private:
    std::string topic_;
    std::uint64_t source_id_;
    std::uint64_t topic_id_;
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

    void reconnect(bool force) {
        const auto now = std::chrono::steady_clock::now();
        for (const auto& a : want_) {
            if (const auto it = conns_.find(a); it != conns_.end() && it->second.alive()) continue;
            if (!force) {
                if (const auto t = last_try_.find(a); t != last_try_.end() && now - t->second < kRetryEvery) continue;
            }
            last_try_[a] = now;
            if (const auto it = conns_.find(a); it != conns_.end()) forget(it);
            try {
                Outgoing c;
                c.stream = net::TcpStream::connect(net::Address::resolve(a), kConnectTimeout);
                if (announcement_) {
                    c.offer(Item{announcement_}, pool_.get());
                    c.step(pool_.get());
                }
                conns_.emplace(a, std::move(c));
            } catch (const net::NetError&) {
                // el suscriptor no está; se reintenta en un rato (o el hub lo quita)
            }
        }
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
                if (const auto now = std::chrono::steady_clock::now(); pool_ && now - last_trim >= std::chrono::seconds(1)) {
                    last_trim = now;
                    (void)pool_->trim(now);
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

    Listener(std::string_view bind, bool can_shm) : listener(net::TcpListener::bind(bind)), shm(can_shm) {}

    void handles(std::vector<std::intptr_t>& out) const {
        out.push_back(listener.native_handle());
        for (const auto& c : conns) {
            if (const auto h = c->handle(); h != -1) out.push_back(h);   // una que ya se cerró, no
        }
    }

    /// Atiende los sockets listos: acepta y lee.
    void service(const std::vector<std::intptr_t>& handles, const bool* is_ready, std::size_t from) {
        if (is_ready[from]) {
            while (auto s = listener.accept()) {
                auto c = std::make_unique<Incoming>();
                c->from = s->peer();
                c->peer->stream = std::move(*s);
                if (shm) {
                    // El suscriptor habla primero (RFC 0009 §3.3): un publicador de
                    // 0.10 nunca lee esto, y uno de 0.11 sabe que puede probar.
                    c->peer->hello = true;
                    c->peer->say(shm::Record{shm::Op::Hello, 0, 0});
                }
                conns.push_back(std::move(c));
            }
        }
        for (std::size_t i = from + 1; i < handles.size(); ++i) {
            if (!is_ready[i]) continue;
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
        topic_id = wire::topic_id(topic);
        if (type.arrow) fanout = std::make_unique<Fanout>(topic, source_id, topic_id);
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
    // Memoria compartida (RFC 0009)
    std::shared_ptr<Lent> lent = std::make_shared<Lent>();
    bool tcp_warned = false;
    bool lent_warned = false;
    std::chrono::steady_clock::time_point last_forget = std::chrono::steady_clock::now();

    void forget_idle_blocks(bool now_please) {
        const auto now = std::chrono::steady_clock::now();
        if (!tcp || (!now_please && now - last_forget < std::chrono::seconds(1))) return;
        last_forget = now;
        tcp->forget_idle(now);
    }

    State(const std::string& w, TypeInfo t)
        : type(std::move(t)), where(w), socket(net::UdpSocket::bind(hub::is_topic(w) ? "127.0.0.1:0" : w)) {
        if (type.arrow) {
            // Los mensajes arrow llegan por TCP: con dirección fija, la misma IP y el mismo puerto.
            tcp = std::make_unique<Listener>(hub::is_topic(w) ? std::string("127.0.0.1:0")
                                                              : host_of(w) + ":" + std::to_string(socket.local_port()),
                                             shm::enabled());
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
        s.forget_idle_blocks(false);
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
            auto got = std::move(s.tcp->ready.front());
            s.tcp->ready.pop_front();
            if (auto out = s.handle_packet(got.packet, std::move(got.from), got.peer)) return out;
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

void SubscriberCore::forget_idle_blocks() { s_->forget_idle_blocks(true); }

std::size_t SubscriberCore::shm_mapped() const { return s_->tcp ? s_->tcp->mapped() : 0; }

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
