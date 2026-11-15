// El núcleo de C++ en modo red (RFC 0011 §2 y §3, parte 4a del bloque 4): el Subscriber pasa a la
// red cuando su hub se lo dice y acepta datos solo de las computadoras declaradas; el Publisher manda
// a destinos de otra computadora. La misma prueba en Python: python/test_red.py.
//
// Un hub de mentira dentro de la prueba (un socket UDP que contesta `ok … peers=…` y `subs …` como el
// hub). Para tener dos computadoras en una se usan IP de 127.0.0.x con AGMEN_NET_IP. Nada espera un
// tiempo fijo: se espera cada condición, con tope.
#include <agmen/agmen.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <cstdlib>

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

using namespace std::chrono_literals;
using agmen::wire::ByteView;
using agmen::wire::Bytes;
namespace net = agmen::net;
namespace hub = agmen::hub;
namespace wire = agmen::wire;

void set_env(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value);
#else
    setenv(name, value, 1);
#endif
}

void unset_env(const char* name) {
#ifdef _WIN32
    _putenv_s(name, "");
#else
    unsetenv(name);
#endif
}

// Un tipo compact (por UDP) y uno arrow (por TCP), escritos a mano (lo que genera agmen gen).
struct Chico {
    static constexpr std::string_view kName = "Chico";
    static constexpr std::string_view kSchema = "Chico{u32 a;u32 b}";
    std::uint32_t a = 0, b = 0;
    void encode_payload(Bytes& out) const {
        wire::put_le(out, a);
        wire::put_le(out, b);
    }
    static Chico decode_payload(ByteView p) {
        return {wire::get_le<std::uint32_t>(p, 0), wire::get_le<std::uint32_t>(p, 4)};
    }
};

struct Grande {
    static constexpr std::string_view kName = "Grande";
    static constexpr std::string_view kSchema = "Grande{u32 w;bytes data}";
    std::uint32_t w = 0;
    std::vector<std::uint8_t> data;

    static const agmen::arrow::Codec& codec() {
        static const agmen::schema::Layout l(kSchema);
        static const agmen::arrow::Codec c(l);
        return c;
    }
    void encode_payload(Bytes& out) const {
        using V = agmen::arrow::Value;
        codec().encode(V::structure({V::uinteger(w), V::blob(data)}), out);
    }
    static Grande decode_payload(ByteView p) {
        const auto v = codec().decode(p);
        Grande m;
        m.w = v.items[0].as<std::uint32_t>();
        m.data.assign(v.items[1].bytes().begin(), v.items[1].bytes().end());
        return m;
    }
};
static_assert(wire::is_arrow_v<Grande>);
static_assert(!wire::is_arrow_v<Chico>);

std::vector<std::uint8_t> big() {
    std::vector<std::uint8_t> out(200 * 1000);   // no cabe en un datagrama
    for (std::size_t i = 0; i < out.size(); ++i) out[i] = static_cast<std::uint8_t>(i * 7);
    return out;
}

/// Espera a que cond() sea verdadero (llamando every() entre intentos); false si se acabó el tope.
bool until(const std::function<bool()>& cond, const std::function<void()>& every = {},
           std::chrono::milliseconds tope = 8000ms) {
    const auto end = std::chrono::steady_clock::now() + tope;
    while (std::chrono::steady_clock::now() < end) {
        if (cond()) return true;
        if (every) {
            every();
        } else {
            std::this_thread::sleep_for(10ms);
        }
    }
    return cond();
}

std::string ip_of(const std::string& from) { return from.substr(0, from.rfind(':')); }

/// Los avisos de Agmen, para revisarlos.
struct Logs {
    std::mutex mu;
    std::vector<std::string> lines;
    Logs() {
        agmen::log::set_sink([this](std::string_view l) {
            std::lock_guard lock(mu);
            lines.emplace_back(l);
        });
    }
    ~Logs() { agmen::log::set_sink({}); }
    std::size_t count(const std::string& part) {
        std::lock_guard lock(mu);
        std::size_t n = 0;
        for (const auto& l : lines) n += l.find(part) != std::string::npos ? 1U : 0U;
        return n;
    }
    std::size_t size() {
        std::lock_guard lock(mu);
        return lines.size();
    }
};

/// Un hub de mentira: registra cada `sub` y `pub`, contesta al `sub` con `ok` (con `peers` si está
/// en modo red) y al `pub` con `subs` (los Subscriber de su topic, más los de "otra computadora").
class FakeHub {
public:
    struct Ep {
        std::string topic;
        std::string host;   // la IP desde la que late
        std::string port;
        std::string tcp;
        bool sub = false;
        bool net = false;
    };

    FakeHub() : sock_(net::UdpSocket::bind("127.0.0.1:0")) {
        addr = "127.0.0.1:" + std::to_string(sock_.local_port());
        thread_ = std::thread([this] { loop(); });
    }
    ~FakeHub() {
        stop_ = true;
        thread_.join();
    }

    void set_peers(std::optional<std::string> p) {
        std::lock_guard lock(mu_);
        peers_ = std::move(p);
    }
    void add_remote(const std::string& topic, const std::string& to, const std::string& tcp = "") {
        std::lock_guard lock(mu_);
        auto& [t, c] = remote_[topic];
        t.push_back(to);
        if (!tcp.empty()) c.push_back(tcp);
    }
    std::optional<Ep> sub_of(const std::string& topic) {
        std::lock_guard lock(mu_);
        for (const auto& [_, e] : eps_) {
            if (e.sub && e.topic == topic) return e;
        }
        return std::nullopt;
    }
    bool any_net() {
        std::lock_guard lock(mu_);
        for (const auto& [_, e] : eps_) {
            if (e.net) return true;
        }
        return false;
    }

    std::string addr;

private:
    net::UdpSocket sock_;
    std::thread thread_;
    std::atomic<bool> stop_{false};
    std::mutex mu_;
    std::optional<std::string> peers_;
    std::map<std::string, Ep> eps_;
    std::map<std::string, std::pair<std::vector<std::string>, std::vector<std::string>>> remote_;

    static std::string join(const std::vector<std::string>& v) {
        std::string out;
        for (const auto& x : v) out += (out.empty() ? "" : ",") + x;
        return out;
    }

    void loop() {
        std::vector<std::uint8_t> buf(65536);
        while (!stop_) {
            const auto rx = sock_.recv_from(buf, 50ms);
            if (!rx) continue;
            std::vector<hub::Line> lines;
            try {
                lines = hub::parse(ByteView(buf.data(), rx->size));
            } catch (const std::invalid_argument&) {
                continue;
            }
            for (const auto& l : lines) {
                if (l.op != "sub" && l.op != "pub") continue;
                std::string reply;
                {
                    std::lock_guard lock(mu_);
                    auto& e = eps_[l.get("id")];
                    e = Ep{l.get("topic"), ip_of(rx->from), l.get("port"), l.get("tcp"), l.op == "sub",
                           l.get("net") == "1"};
                    if (e.sub) {
                        std::vector<std::pair<std::string, std::string>> f{{"id", l.get("id")}};
                        if (peers_) f.emplace_back("peers", *peers_);
                        reply = hub::line("ok", f);
                    } else {
                        std::vector<std::string> to;
                        std::vector<std::string> tcp;
                        for (const auto& [_, s] : eps_) {
                            if (!s.sub || s.topic != e.topic) continue;
                            to.push_back(s.host + ":" + s.port);
                            if (!s.tcp.empty()) tcp.push_back(s.host + ":" + s.tcp);
                        }
                        if (const auto it = remote_.find(e.topic); it != remote_.end()) {
                            to.insert(to.end(), it->second.first.begin(), it->second.first.end());
                            tcp.insert(tcp.end(), it->second.second.begin(), it->second.second.end());
                        }
                        reply = hub::line("subs", {{"id", l.get("id")}, {"to", join(to)}, {"tcp", join(tcp)}});
                    }
                }
                (void)sock_.send_to(ByteView(reinterpret_cast<const std::uint8_t*>(reply.data()), reply.size()),
                                    net::Address::resolve(rx->from));
            }
        }
    }
};

/// Abre algo de la "computadora" de `h` (con su AGMEN_NET_IP, o sin ella).
template <typename T>
std::unique_ptr<T> opened(const FakeHub& h, const std::string& where, const char* net_ip = nullptr) {
    set_env("AGMEN_HUB", h.addr.c_str());
    if (net_ip != nullptr) {
        set_env("AGMEN_NET_IP", net_ip);
    } else {
        unset_env("AGMEN_NET_IP");
    }
    std::unique_ptr<T> out;
    try {
        out = std::make_unique<T>(where);
    } catch (...) {
        unset_env("AGMEN_NET_IP");
        throw;
    }
    unset_env("AGMEN_NET_IP");
    return out;
}

template <typename M>
void receive_all(agmen::Subscriber<M>& sub, std::vector<agmen::Sample<M>>& out, std::chrono::milliseconds t = 20ms) {
    while (auto s = sub.receive(t)) out.push_back(std::move(*s));
}

/// ¿Se puede abrir un UDP en `addr`? (el socket que estaba ahí ya se cerró)
bool udp_free(const std::string& addr) {
    try {
        (void)net::UdpSocket::bind(addr);
        return true;
    } catch (const net::NetError&) {
        return false;
    }
}

/// ¿Nadie escucha TCP en `addr`?
bool tcp_closed(const std::string& addr) {
    try {
        (void)net::TcpStream::connect(net::Address::resolve(addr), 500ms);
        return false;
    } catch (const net::NetError&) {
        return true;
    }
}

/// Un datagrama de datos de Chico, armado a mano (para mandarlo desde la IP que se quiera).
Bytes chico_packet(const std::string& topic, std::uint32_t a, std::uint32_t b) {
    Bytes payload;
    Chico{a, b}.encode_payload(payload);
    const wire::Envelope env{.encoding = wire::Encoding::Compact,
                             .schema_id = wire::schema_id_v<Chico>,
                             .topic_id = wire::topic_id(topic),
                             .source_id = 42,
                             .seq = a,
                             .t_data = wire::now_ns(),
                             .t_pub = wire::now_ns(),
                             .payload_len = static_cast<std::uint32_t>(payload.size()),
                             .clock = agmen::clock::domain()};
    Bytes packet;
    env.write_to(packet);
    packet.insert(packet.end(), payload.begin(), payload.end());
    return packet;
}

// ---------------------------------------------------------------------------

void test_sin_modo_red_nada_cambia() {
    Logs logs;
    FakeHub h;
    auto sub = opened<agmen::Subscriber<Chico>>(h, "/l");
    auto subg = opened<agmen::Subscriber<Grande>>(h, "/lg");
    auto pub = opened<agmen::Publisher<Chico>>(h, "/l");
    auto pubg = opened<agmen::Publisher<Grande>>(h, "/lg");
    std::vector<agmen::Sample<Chico>> got;
    std::vector<agmen::Sample<Grande>> gotg;
    const auto data = big();
    CHECK(until([&] { return got.size() >= 3 && gotg.size() >= 3; }, [&] {
        (void)pub->publish(Chico{1, 2});
        (void)pubg->publish(Grande{1, data});
        receive_all(*sub, got);
        receive_all(*subg, gotg);
    }));
    CHECK(sub->listen_ip() == "127.0.0.1");
    CHECK(subg->listen_ip() == "127.0.0.1");
    CHECK(sub->native_handles().size() == 1);   // un solo socket, como siempre
    CHECK(sub->rejected() + subg->rejected() == 0);
    CHECK(pub->unreachable() + pubg->unreachable() == 0);
    CHECK(!h.any_net());
    CHECK(!got.empty() && ip_of(got[0].from) == "127.0.0.1");
    CHECK(logs.size() == 0);
}

void test_el_subscriber_pasa_a_la_red_sin_perder_ni_cortar() {
    Logs logs;
    FakeHub h;
    auto sub = opened<agmen::Subscriber<Grande>>(h, "/g");
    auto pub = opened<agmen::Publisher<Grande>>(h, "/g");
    const auto old_port = sub->local_port();
    const auto old_tcp = sub->tcp_port();
    // Una conexión TCP aceptada antes del cambio (un Publisher por dirección al puerto viejo).
    agmen::Publisher<Grande> direct("127.0.0.1:" + std::to_string(old_tcp), "/g");
    std::vector<agmen::Sample<Grande>> got;
    std::uint32_t k = 0;
    const auto data = big();
    const auto step = [&] {
        (void)pub->publish(Grande{k, data});
        ++k;
        receive_all(*sub, got);
    };
    CHECK(until([&] { return got.size() >= 3; }, step));
    h.set_peers("192.0.2.10");
    // El primer `ok` con peers: sockets nuevos en todas las interfaces, registro nuevo con net=1.
    CHECK(until([&] { const auto e = h.sub_of("/g"); return e && e->net; }, step));
    CHECK(sub->listen_ip() == "0.0.0.0");
    CHECK(sub->local_port() != old_port);
    const auto e = h.sub_of("/g");
    CHECK(e && e->port == std::to_string(sub->local_port()) && e->tcp == std::to_string(sub->tcp_port()));
    CHECK(e && e->host == "127.0.0.1");
    // Los de antes se leen hasta 1 s después de la confirmación, y se cierran.
    CHECK(until([&] { return udp_free("127.0.0.1:" + std::to_string(old_port)); }, step));
    CHECK(until([&] { return tcp_closed("127.0.0.1:" + std::to_string(old_tcp)); }, step));
    for (int i = 0; i < 5; ++i) step();
    CHECK(until([&] { return !got.empty() && got.back().message.w == k - 1; }, [&] { receive_all(*sub, got); }));
    bool in_order = got.size() == k;   // nada perdido por TCP
    for (std::size_t i = 0; in_order && i < got.size(); ++i) in_order = got[i].message.w == i;
    CHECK(in_order);
    // La conexión aceptada antes del cambio sigue viva y entrega.
    (void)direct.publish(Grande{777, {1}});
    CHECK(until([&] { return !got.empty() && got.back().message.w == 777; }, [&] { receive_all(*sub, got); }));
    CHECK(sub->rejected() == 0);
}

void test_compact_tambien_pasa_y_cerrar_cierra_todo() {
    Logs logs;
    FakeHub h;
    auto sub = opened<agmen::Subscriber<Chico>>(h, "/c");
    auto pub = opened<agmen::Publisher<Chico>>(h, "/c");
    std::vector<agmen::Sample<Chico>> got;
    const auto old_port = sub->local_port();
    h.set_peers("192.0.2.10");
    CHECK(until([&] { return sub->native_handles().size() == 2; }, [&] {
        (void)pub->publish(Chico{1, 1});
        receive_all(*sub, got);
    }));
    const auto new_port = sub->local_port();
    CHECK(new_port != old_port);
    sub.reset();   // a media transición: cierra los dos
    CHECK(udp_free("127.0.0.1:" + std::to_string(old_port)));
    CHECK(udp_free("0.0.0.0:" + std::to_string(new_port)));
    CHECK(!got.empty());
}

void test_acepta_lo_local_y_a_peers_y_descarta_lo_demas() {
    Logs logs;
    FakeHub h;
    auto sub = opened<agmen::Subscriber<Chico>>(h, "/f");
    auto subg = opened<agmen::Subscriber<Grande>>(h, "/fg");
    h.set_peers("127.0.0.4");
    CHECK(until([&] { return sub->listen_ip() == "0.0.0.0" && subg->listen_ip() == "0.0.0.0"; }, [&] {
        (void)sub->receive(20ms);
        (void)subg->receive(20ms);
    }));
    auto local = net::UdpSocket::bind("127.0.0.1:0");
    auto par = net::UdpSocket::bind("127.0.0.4:0");
    auto otra = net::UdpSocket::bind("127.0.0.3:0");
    const auto dest = net::Address::resolve("127.0.0.1:" + std::to_string(sub->local_port()));
    const auto ronda = [&](std::uint32_t n, bool todos = true) {
        std::uint32_t i = 0;
        for (auto* s : {&local, &otra, &par}) {
            if (todos || s == &local) (void)s->send_to(chico_packet("/f", n, i), dest);
            ++i;
        }
        std::vector<agmen::Sample<Chico>> out;
        receive_all(*sub, out, 100ms);
        return out;
    };
    auto got = ronda(1);
    const auto got2 = ronda(2);
    got.insert(got.end(), got2.begin(), got2.end());
    std::vector<std::pair<std::string, std::uint32_t>> seen;
    for (const auto& s : got) seen.emplace_back(ip_of(s.from), s.message.a);
    std::sort(seen.begin(), seen.end());
    CHECK((seen == std::vector<std::pair<std::string, std::uint32_t>>{
                       {"127.0.0.1", 1}, {"127.0.0.1", 2}, {"127.0.0.4", 1}, {"127.0.0.4", 2}}));
    CHECK(sub->rejected() == 2);
    const std::string aviso =
        "[agmen] '/f': llegó un dato de 127.0.0.3, que no es una computadora de [[machine]]: lo descarté "
        "(¿otra red, otro robot?)";
    CHECK(logs.count(aviso) == 1);   // una vez por IP
    // TCP: una conexión de otra IP se cierra y se cuenta.
    bool pending = false;
    auto c = net::TcpStream::connect_async(net::Address::resolve("127.0.0.1:" + std::to_string(subg->tcp_port())),
                                           "127.0.0.3", pending);
    std::uint8_t one[1];
    CHECK(until([&] { (void)subg->receive(20ms); return c.recv_some(one) < 0; }));
    CHECK(subg->rejected() == 1);
    CHECK(logs.count("'/fg': llegó un dato de 127.0.0.3") == 1);
    // El hub vuelve a modo local: mismos sockets, pero solo lo local.
    h.set_peers(std::nullopt);
    CHECK(until([&] { return sub->rejected() > 2; }, [&] {
        (void)par.send_to(chico_packet("/f", 9, 9), dest);
        std::vector<agmen::Sample<Chico>> out;
        receive_all(*sub, out, 50ms);
    }));
    const auto before = sub->rejected();
    const auto last = ronda(3);
    CHECK(last.size() == 1 && ip_of(last[0].from) == "127.0.0.1" && last[0].message.a == 3);
    CHECK(sub->rejected() == before + 2);
    CHECK(sub->listen_ip() == "0.0.0.0");
}

void test_tope_de_avisos() {
    Logs logs;
    FakeHub h;
    auto sub = opened<agmen::Subscriber<Chico>>(h, "/t");
    h.set_peers("127.0.0.4");
    CHECK(until([&] { return sub->listen_ip() == "0.0.0.0"; }, [&] { (void)sub->receive(20ms); }));
    const auto dest = net::Address::resolve("127.0.0.1:" + std::to_string(sub->local_port()));
    const auto packet = chico_packet("/t", 1, 1);
    constexpr int kIps = 260;
    for (int i = 0; i < kIps; ++i) {
        const std::string ip = i < 250 ? "127.0.1." + std::to_string(i + 1) : "127.0.2." + std::to_string(i - 249);
        auto s = net::UdpSocket::bind(ip + ":0");
        (void)s.send_to(packet, dest);
        if (i % 20 == 19) {
            std::vector<agmen::Sample<Chico>> out;
            receive_all(*sub, out, 5ms);
        }
    }
    CHECK(until([&] { return sub->rejected() == kIps; }, [&] {
        std::vector<agmen::Sample<Chico>> out;
        receive_all(*sub, out, 20ms);
    }));
    CHECK(logs.count("que no es una computadora de [[machine]]") == 256);
}

/// "a" es 127.0.0.3 y "b" es 127.0.0.2 (AGMEN_NET_IP), cada una con su hub.
struct DosComputadoras {
    FakeHub ha, hb;
    std::unique_ptr<agmen::Subscriber<Chico>> sub;
    std::unique_ptr<agmen::Subscriber<Grande>> subg;

    DosComputadoras() {
        hb.set_peers("127.0.0.3");
        sub = opened<agmen::Subscriber<Chico>>(hb, "/x", "127.0.0.2");
        subg = opened<agmen::Subscriber<Grande>>(hb, "/xg", "127.0.0.2");
        CHECK(until(
            [&] {
                const auto a = hb.sub_of("/x");
                const auto b = hb.sub_of("/xg");
                return a && a->net && b && b->net;
            },
            [&] {
                (void)sub->receive(20ms);
                (void)subg->receive(20ms);
            }));
        CHECK(sub->listen_ip() == "127.0.0.2");
        CHECK(subg->listen_ip() == "127.0.0.2");
        const auto e = hb.sub_of("/x");
        CHECK(e && e->host == "127.0.0.2" && e->port == std::to_string(sub->local_port()));
        ha.add_remote("/x", "127.0.0.2:" + std::to_string(sub->local_port()));
        ha.add_remote("/xg", "127.0.0.2:" + std::to_string(subg->local_port()),
                      "127.0.0.2:" + std::to_string(subg->tcp_port()));
    }
};

void test_chico_y_grande_cruzan() {
    Logs logs;
    DosComputadoras d;
    auto pub = opened<agmen::Publisher<Chico>>(d.ha, "/x", "127.0.0.3");
    auto pubg = opened<agmen::Publisher<Grande>>(d.ha, "/xg", "127.0.0.3");
    std::vector<agmen::Sample<Chico>> got;
    std::vector<agmen::Sample<Grande>> gotg;
    const auto data = big();
    CHECK(until([&] { return !got.empty() && !gotg.empty(); }, [&] {
        (void)pub->publish(Chico{5, 6});
        (void)pubg->publish(Grande{7, data});
        receive_all(*d.sub, got);
        receive_all(*d.subg, gotg);
    }));
    CHECK(!got.empty() && got[0].message.a == 5 && got[0].message.b == 6);
    CHECK(!got.empty() && ip_of(got[0].from) == "127.0.0.3");   // salió desde AGMEN_NET_IP
    CHECK(!gotg.empty() && gotg[0].message.w == 7 && gotg[0].message.data == data);
    CHECK(!gotg.empty() && ip_of(gotg[0].from) == "127.0.0.3");
    // Sin memoria compartida hacia otra computadora: ni se prueba ni se avisa.
    CHECK(pubg->shm_subscribers() == 0);
    CHECK(pubg->shm_bytes() == 0);
    CHECK(logs.count("memoria compartida") == 0);
    CHECK(d.sub->rejected() + d.subg->rejected() == 0);
    CHECK(pub->unreachable() + pubg->unreachable() == 0);
}

void test_un_destino_inalcanzable_no_frena_a_los_demas() {
    Logs logs;
    DosComputadoras d;
    d.ha.add_remote("/x", "192.0.2.1:7600");
    d.ha.add_remote("/xg", "192.0.2.1:7600", "192.0.2.1:7601");
    auto pub = opened<agmen::Publisher<Chico>>(d.ha, "/x", "127.0.0.3");
    auto pubg = opened<agmen::Publisher<Grande>>(d.ha, "/xg", "127.0.0.3");
    CHECK(until([&] { return pub->subscribers() == 2 && pubg->subscribers() == 2; }, [&] {
        (void)pub->publish(Chico{0, 0});
        (void)pubg->publish(Grande{0, {}});
        std::this_thread::sleep_for(10ms);
    }));
    std::vector<agmen::Sample<Chico>> got;
    std::vector<agmen::Sample<Grande>> gotg;
    receive_all(*d.sub, got);
    receive_all(*d.subg, gotg);
    const auto data = big();
    auto worst = std::chrono::steady_clock::duration::zero();
    for (std::uint32_t k = 1; k <= 30; ++k) {
        const auto t = std::chrono::steady_clock::now();
        (void)pub->publish(Chico{k, 0});
        (void)pubg->publish(Grande{k, data});
        worst = std::max(worst, std::chrono::steady_clock::now() - t);
        receive_all(*d.sub, got);
        receive_all(*d.subg, gotg);
    }
    CHECK(until([&] { return pubg->unreachable() > 0; }, [&] {
        (void)pubg->publish(Grande{99, {}});
        std::this_thread::sleep_for(20ms);
    }));
    receive_all(*d.sub, got, 100ms);
    receive_all(*d.subg, gotg, 100ms);
    std::vector<std::uint32_t> a, w, want;
    for (const auto& s : got) {
        if (s.message.a != 0) a.push_back(s.message.a);
    }
    for (const auto& s : gotg) {
        if (s.message.w > 0 && s.message.w < 99) w.push_back(s.message.w);
    }
    for (std::uint32_t k = 1; k <= 30; ++k) want.push_back(k);
    CHECK(a == want);
    CHECK(w == want);
    CHECK(worst < 500ms);   // publish() no se queda esperando
    CHECK(logs.count("no pude mandar a la computadora 192.0.2.1 (192.0.2.1:7601)") == 1);   // una vez por destino
#ifdef __linux__
    // Linux: un socket atado a 127.0.0.x no sale de lo (EINVAL).
    CHECK(pub->unreachable() > 0);
    CHECK(logs.count("no pude mandar a la computadora 192.0.2.1 (192.0.2.1:7600)") == 1);
#endif
}

void test_agmen_net_ip_que_no_sirve() {
    FakeHub h;
    const auto expect = [&](auto open) {
        bool invalid = false;
        try {
            open("300.1.1.1");
        } catch (const std::invalid_argument& e) {
            invalid = std::string(e.what()).find("AGMEN_NET_IP=300.1.1.1") != std::string::npos;
        }
        CHECK(invalid);
        bool foreign = false;
        try {
            open("192.0.2.77");
        } catch (const net::NetError& e) {
            foreign = std::string(e.what()).find("AGMEN_NET_IP=192.0.2.77 no es una IP de esta computadora") !=
                      std::string::npos;
        }
        CHECK(foreign);
    };
    expect([&](const char* ip) { (void)opened<agmen::Subscriber<Chico>>(h, "/n", ip); });
    expect([&](const char* ip) { (void)opened<agmen::Publisher<Chico>>(h, "/n", ip); });
    // Por dirección no se mira (no es modo topic).
    set_env("AGMEN_NET_IP", "300.1.1.1");
    bool ok = true;
    try {
        agmen::Subscriber<Chico> by_addr("127.0.0.1:0");
        ok = by_addr.listen_ip() == "127.0.0.1";
    } catch (...) {
        ok = false;
    }
    unset_env("AGMEN_NET_IP");
    CHECK(ok);
}

}  // namespace

int main() {
    test_sin_modo_red_nada_cambia();
    test_el_subscriber_pasa_a_la_red_sin_perder_ni_cortar();
    test_compact_tambien_pasa_y_cerrar_cierra_todo();
    test_acepta_lo_local_y_a_peers_y_descarta_lo_demas();
    test_tope_de_avisos();
    test_chico_y_grande_cruzan();
    test_un_destino_inalcanzable_no_frena_a_los_demas();
    test_agmen_net_ip_que_no_sirve();
    if (g_failures == 0) std::printf("test_red: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
