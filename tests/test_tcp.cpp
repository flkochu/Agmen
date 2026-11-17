// El transporte de los mensajes grandes (RFC 0008, D2 y D3): los tipos arrow
// viajan por TCP, un suscriptor lento recibe solo lo más nuevo y no frena a
// nadie. La misma prueba en Python: python/test_tcp.py.
#include <agmen/agmen.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifndef _WIN32
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

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

// Un tipo con un campo variable, escrito a mano (lo que genera agmen gen).
struct Img {
    static constexpr std::string_view kName = "Img";
    static constexpr std::string_view kSchema = "Img{u32 w;u32 h;bytes data}";
    std::uint32_t w = 0;
    std::uint32_t h = 0;
    std::vector<std::uint8_t> data;

    static const agmen::arrow::Codec& codec() {
        static const agmen::schema::Layout l(kSchema);
        static const agmen::arrow::Codec c(l);
        return c;
    }
    void encode_payload(Bytes& out) const {
        using V = agmen::arrow::Value;
        codec().encode(V::structure({V::uinteger(w), V::uinteger(h), V::blob(data)}), out);
    }
    static Img decode_payload(ByteView p) {
        const auto v = codec().decode(p);
        Img m;
        m.w = v.items[0].as<std::uint32_t>();
        m.h = v.items[1].as<std::uint32_t>();
        m.data.assign(v.items[2].bytes().begin(), v.items[2].bytes().end());
        return m;
    }
};
static_assert(agmen::wire::is_arrow_v<Img>);

std::vector<std::uint8_t> big() {
    std::vector<std::uint8_t> out(1024 * 1000);   // 1 MB: no cabe en un datagrama
    for (std::size_t i = 0; i < out.size(); ++i) out[i] = static_cast<std::uint8_t>(i);
    return out;
}

void test_un_megabyte_llega_entero() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    CHECK(sub.native_handles().size() >= 2);   // UDP + el puerto TCP
    CHECK(sub.tcp_port() == sub.local_port());  // con dirección fija, el mismo puerto en TCP
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const auto data = big();
    CHECK(pub.publish(Img{640, 480, data}) == agmen::net::SendStatus::Ok);
    const auto s = sub.receive(2000ms);
    CHECK(s.has_value());
    if (s) {
        CHECK(s->envelope.encoding == agmen::wire::Encoding::Arrow);
        CHECK(s->message.w == 640 && s->message.h == 480);
        CHECK(s->message.data == data);
    }
    CHECK(sub.announced().count(agmen::wire::schema_id_v<Img>) == 1);
}

void test_suscriptor_lento_recibe_lo_mas_nuevo() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const auto data = big();
    const auto t0 = std::chrono::steady_clock::now();
    for (std::uint32_t k = 0; k < 20; ++k) (void)pub.publish(Img{k, 0, data});   // nadie lee mientras tanto
    CHECK(std::chrono::steady_clock::now() - t0 < 1s);   // publicar no espera al suscriptor
    std::size_t got = 0;
    std::uint32_t last = 0;
    while (const auto s = sub.receive(300ms)) {
        ++got;
        last = s->message.w;
    }
    CHECK(got >= 1 && got < 20);
    CHECK(last == 19);   // el último siempre llega
    CHECK(got + pub.dropped() == 20);
}

void test_muchos_chicos_en_orden() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    for (std::uint32_t k = 0; k < 200; ++k) (void)pub.publish(Img{k, 0, std::vector<std::uint8_t>(k % 7, 1)});
    std::uint32_t expect = 0;
    while (const auto s = sub.receive(300ms)) {
        CHECK(s->message.w == expect);
        ++expect;
    }
    CHECK(expect == 200);
    CHECK(pub.dropped() == 0);
}

// ---------------------------------------------------------------------------
// Un Publisher nunca se queda esperando a que un suscriptor conteste la conexión TCP.
// ---------------------------------------------------------------------------

namespace net = agmen::net;
namespace hub = agmen::hub;

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

/// Un puerto TCP que no contesta. En Linux, uno que escucha con la cola de conexiones llena: el sistema
/// descarta el SYN y quien se conecta se queda esperando (como una computadora que no contesta). En
/// Windows, un puerto sin nadie que escuche: ahí tarda ~1 s en rechazar.
class DestinoMuerto {
public:
    DestinoMuerto() {
#ifdef _WIN32
        auto l = net::TcpListener::bind("127.0.0.1:0");
        port_ = l.local_port();   // se cierra al salir: nadie escucha ahí
#else
        const int srv = ::socket(AF_INET, SOCK_STREAM, 0);
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        socklen_t len = sizeof addr;
        if (srv < 0 || ::bind(srv, reinterpret_cast<const sockaddr*>(&addr), sizeof addr) != 0 || ::listen(srv, 0) != 0 ||
            ::getsockname(srv, reinterpret_cast<sockaddr*>(&addr), &len) != 0) {
            std::fprintf(stderr, "  no pude abrir el destino muerto\n");
            std::abort();
        }
        port_ = ntohs(addr.sin_port);
        fds_.push_back(srv);
        for (int i = 0; i < 3; ++i) {   // llenan la cola (nadie las acepta)
            const int c = ::socket(AF_INET, SOCK_STREAM, 0);
            (void)::fcntl(c, F_SETFL, ::fcntl(c, F_GETFL) | O_NONBLOCK);
            (void)::connect(c, reinterpret_cast<const sockaddr*>(&addr), sizeof addr);
            fds_.push_back(c);
        }
#endif
    }
    DestinoMuerto(const DestinoMuerto&) = delete;
    DestinoMuerto& operator=(const DestinoMuerto&) = delete;
    ~DestinoMuerto() {
#ifndef _WIN32
        for (const int fd : fds_) ::close(fd);
#endif
    }
    [[nodiscard]] std::string addr() const { return "127.0.0.1:" + std::to_string(port_); }

private:
    std::uint16_t port_ = 0;
#ifndef _WIN32
    std::vector<int> fds_;
#endif
};

/// Un hub de mentira: al `pub` le contesta `subs` con la lista TCP que diga la prueba y, si se le pide,
/// se la repite cada tanto (el hub de verdad la manda en cada latido).
class HubFalso {
public:
    HubFalso() : sock_(net::UdpSocket::bind("127.0.0.1:0")) {
        addr = "127.0.0.1:" + std::to_string(sock_.local_port());
        thread_ = std::thread([this] { loop(); });
    }
    HubFalso(const HubFalso&) = delete;
    HubFalso& operator=(const HubFalso&) = delete;
    ~HubFalso() {
        stop_ = true;
        thread_.join();
    }

    void lista(std::vector<std::string> tcp, std::optional<std::chrono::milliseconds> cada = std::nullopt) {
        {
            std::lock_guard lock(mu_);
            tcp_ = std::move(tcp);
            cada_ = cada;
        }
        push();
    }

    std::string addr;

private:
    net::UdpSocket sock_;
    std::thread thread_;
    std::atomic<bool> stop_{false};
    std::mutex mu_;
    std::vector<std::string> tcp_;
    std::optional<std::chrono::milliseconds> cada_;
    std::optional<std::pair<std::string, std::string>> pub_;   // id y dirección del Publisher

    void push() {
        std::string text;
        std::string to;
        {
            std::lock_guard lock(mu_);
            if (!pub_) return;
            std::string joined;
            for (const auto& x : tcp_) joined += (joined.empty() ? "" : ",") + x;
            text = hub::line("subs", {{"id", pub_->first}, {"topic", "/cam"}, {"to", joined}, {"tcp", joined}});
            to = pub_->second;
        }
        (void)sock_.send_to(ByteView(reinterpret_cast<const std::uint8_t*>(text.data()), text.size()),
                            net::Address::resolve(to));
    }

    void loop() {
        std::vector<std::uint8_t> buf(65536);
        auto pushed = std::chrono::steady_clock::now();
        while (!stop_) {
            if (const auto rx = sock_.recv_from(buf, 10ms)) {
                std::vector<hub::Line> lines;
                try {
                    lines = hub::parse(ByteView(buf.data(), rx->size));
                } catch (const std::invalid_argument&) {
                }
                for (const auto& l : lines) {
                    if (l.op != "pub") continue;
                    {
                        std::lock_guard lock(mu_);
                        pub_ = std::make_pair(l.get("id"), rx->from);
                    }
                    push();
                }
            }
            std::optional<std::chrono::milliseconds> cada;
            {
                std::lock_guard lock(mu_);
                cada = cada_;
            }
            if (cada && std::chrono::steady_clock::now() - pushed >= *cada) {
                pushed = std::chrono::steady_clock::now();
                push();
            }
        }
    }
};

/// Un Publisher por topic que habla con `h`.
std::unique_ptr<agmen::Publisher<Img>> publisher_de(const HubFalso& h) {
    set_env("AGMEN_HUB", h.addr.c_str());
    auto out = std::make_unique<agmen::Publisher<Img>>("/cam");
    unset_env("AGMEN_HUB");
    return out;
}

double segundos(std::chrono::steady_clock::duration d) { return std::chrono::duration<double>(d).count(); }

void test_publicar_no_espera_a_un_suscriptor_que_no_contesta() {
    HubFalso h;
    DestinoMuerto m1;
    DestinoMuerto m2;
    DestinoMuerto m3;
    agmen::Subscriber<Img> vivo("127.0.0.1:0");
    auto pub = publisher_de(h);
    h.lista({m1.addr(), m2.addr(), m3.addr(), "127.0.0.1:" + std::to_string(vivo.tcp_port())});
    std::this_thread::sleep_for(50ms);   // la lista ya está en el socket: la lee el primer publish()
    std::chrono::steady_clock::duration peor{};
    std::uint32_t n = 0;
    const auto inicio = std::chrono::steady_clock::now();
    while (n < 150 && std::chrono::steady_clock::now() - inicio < 5s) {   // ~1.5 s: pasa el plazo y un reintento
        const auto t = std::chrono::steady_clock::now();
        (void)pub->publish(Img{n, 0, std::vector<std::uint8_t>(100, 1)});
        peor = std::max(peor, std::chrono::steady_clock::now() - t);
        ++n;
        std::this_thread::sleep_for(10ms);
    }
    std::printf("  peor publish() con 3 destinos muertos: %.4f s\n", segundos(peor));
    CHECK(pub->subscribers() == 4);
    CHECK(peor < 250ms);
    std::uint32_t expect = 0;
    while (const auto s = vivo.receive(300ms)) {
        CHECK(s->message.w == expect);   // el vivo recibe todo, en orden
        ++expect;
    }
    CHECK(expect == n);
    auto t = std::chrono::steady_clock::now();
    const auto dropped = pub->dropped();
    CHECK(std::chrono::steady_clock::now() - t < 200ms);
    CHECK(dropped == 0);
    t = std::chrono::steady_clock::now();
    pub.reset();   // close()
    const auto cierre = std::chrono::steady_clock::now() - t;
    std::printf("  close(): %.4f s\n", segundos(cierre));
    CHECK(cierre < 200ms);
}

void test_la_misma_lista_no_reintenta() {
    // Un suscriptor que acepta y cierra en seguida: el Publisher pierde la conexión cada vez. El hub repite
    // la misma lista cada 20 ms; eso no debe forzar una conexión nueva: se reintenta cada 1 s.
    HubFalso h;
    auto srv = net::TcpListener::bind("127.0.0.1:0");
    std::atomic<int> intentos{0};
    std::atomic<bool> alto{false};
    std::thread acepta([&] {
        while (!alto) {
            if (auto c = srv.accept()) {
                ++intentos;
                c->close();
            } else {
                std::this_thread::sleep_for(2ms);
            }
        }
    });
    auto pub = publisher_de(h);
    h.lista({"127.0.0.1:" + std::to_string(srv.local_port())}, 20ms);
    const auto t0 = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - t0 < 800ms) {
        (void)pub->publish(Img{1, 0, {1}});
        std::this_thread::sleep_for(10ms);
    }
    std::this_thread::sleep_for(50ms);
    const double pasaron = segundos(std::chrono::steady_clock::now() - t0);
    const int n_intentos = intentos.load();
    std::printf("  %d conexiones en %.2f s\n", n_intentos, pasaron);
    CHECK(n_intentos >= 1);
    CHECK(n_intentos <= 2 + static_cast<int>(pasaron / 1.0));
    pub.reset();
    alto = true;
    acepta.join();
}

}  // namespace

int main() {
    test_un_megabyte_llega_entero();
    test_suscriptor_lento_recibe_lo_mas_nuevo();
    test_muchos_chicos_en_orden();
    test_publicar_no_espera_a_un_suscriptor_que_no_contesta();
    test_la_misma_lista_no_reintenta();
    if (g_failures == 0) std::printf("test_tcp: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
