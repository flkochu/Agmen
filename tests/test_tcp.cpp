// El transporte de los mensajes grandes (RFC 0008, D2 y D3): los tipos arrow
// viajan por TCP, un suscriptor lento recibe solo lo más nuevo y no frena a
// nadie. La misma prueba en Python: python/test_tcp.py.
#include <agmen/agmen.hpp>
#include <agmen/log.hpp>
#include <agmen/platform.hpp>

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

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
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

#ifdef _WIN32
using Fd = SOCKET;
const Fd kSinFd = INVALID_SOCKET;
void cerrar(Fd fd) { (void)::closesocket(fd); }
void sin_bloqueo(Fd fd) {
    u_long si = 1;
    (void)::ioctlsocket(fd, FIONBIO, &si);
}
#else
using Fd = int;
const Fd kSinFd = -1;
void cerrar(Fd fd) { (void)::close(fd); }
void sin_bloqueo(Fd fd) { (void)::fcntl(fd, F_SETFL, ::fcntl(fd, F_GETFL) | O_NONBLOCK); }
#endif

/// Un puerto TCP que no contesta. En Linux, uno que escucha con la cola de conexiones llena: el sistema
/// descarta el SYN y quien se conecta se queda esperando (como una computadora que no contesta). En
/// Windows, un puerto atado sin listen(): nadie acepta (ahí tarda ~1 s en rechazar) y nadie más lo toma.
/// revivir() lo vuelve un puerto que acepta: así se ve si el Publisher lo reintenta.
class DestinoMuerto {
public:
    DestinoMuerto() {
#ifdef _WIN32
        WSADATA wsa{};
        (void)::WSAStartup(MAKEWORD(2, 2), &wsa);   // ya lo hizo el núcleo; aquí solo suma una referencia
#endif
        srv_ = ::socket(AF_INET, SOCK_STREAM, 0);
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        socklen_t len = static_cast<socklen_t>(sizeof addr);
        if (srv_ == kSinFd || ::bind(srv_, reinterpret_cast<const sockaddr*>(&addr), len) != 0 ||
            ::getsockname(srv_, reinterpret_cast<sockaddr*>(&addr), &len) != 0) {
            std::fprintf(stderr, "  no pude abrir el destino muerto\n");
            std::abort();
        }
        port_ = ntohs(addr.sin_port);
#ifndef _WIN32
        if (::listen(srv_, 0) != 0) {
            std::fprintf(stderr, "  no pude abrir el destino muerto\n");
            std::abort();
        }
        for (int i = 0; i < 3; ++i) {   // llenan la cola (nadie las acepta)
            const Fd c = ::socket(AF_INET, SOCK_STREAM, 0);
            sin_bloqueo(c);
            (void)::connect(c, reinterpret_cast<const sockaddr*>(&addr), sizeof addr);
            sockaddr_in mio{};
            socklen_t n = static_cast<socklen_t>(sizeof mio);
            if (::getsockname(c, reinterpret_cast<sockaddr*>(&mio), &n) == 0) de_tapon_.push_back(ntohs(mio.sin_port));
            tapan_.push_back(c);
        }
#endif
    }
    DestinoMuerto(const DestinoMuerto&) = delete;
    DestinoMuerto& operator=(const DestinoMuerto&) = delete;
    ~DestinoMuerto() {
        for (const Fd fd : tapan_) cerrar(fd);
        for (const Fd fd : aceptadas_) cerrar(fd);
        cerrar(srv_);
#ifdef _WIN32
        (void)::WSACleanup();
#endif
    }
    [[nodiscard]] std::string addr() const { return "127.0.0.1:" + std::to_string(port_); }

    /// Desde ya acepta conexiones (las cuenta atender()).
    void revivir() {
        for (const Fd fd : tapan_) cerrar(fd);
        tapan_.clear();
#ifdef _WIN32
        (void)::listen(srv_, 16);
#endif
        sin_bloqueo(srv_);
    }

    /// Acepta lo que haya llegado y devuelve cuántas conexiones del Publisher lleva.
    std::size_t atender() {
        for (;;) {
            sockaddr_in peer{};
            socklen_t n = static_cast<socklen_t>(sizeof peer);
            const Fd c = ::accept(srv_, reinterpret_cast<sockaddr*>(&peer), &n);
            if (c == kSinFd) break;
            if (std::find(de_tapon_.begin(), de_tapon_.end(), ntohs(peer.sin_port)) != de_tapon_.end()) {
                cerrar(c);   // una de las que llenaban la cola
            } else {
                aceptadas_.push_back(c);
            }
        }
        return aceptadas_.size();
    }

private:
    Fd srv_ = kSinFd;
    std::uint16_t port_ = 0;
    std::vector<Fd> tapan_;
    std::vector<Fd> aceptadas_;
    std::vector<std::uint16_t> de_tapon_;   // puertos de origen de las que llenan la cola
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
/// AGMEN_HUB queda como estaba.
std::unique_ptr<agmen::Publisher<Img>> publisher_de(const HubFalso& h) {
    const std::string antes = agmen::platform::env("AGMEN_HUB");
    set_env("AGMEN_HUB", h.addr.c_str());
    auto out = std::make_unique<agmen::Publisher<Img>>("/cam");
    if (antes.empty()) {
        unset_env("AGMEN_HUB");
    } else {
        set_env("AGMEN_HUB", antes.c_str());
    }
    return out;
}

double segundos(std::chrono::steady_clock::duration d) { return std::chrono::duration<double>(d).count(); }

void test_publicar_no_espera_a_un_suscriptor_que_no_contesta() {
    // Corre lo bastante para ver el plazo de conexión (1 s) vencer y el reintento (1 s después del fallo),
    // ~2 s: ni la primera espera ni el reintento pueden frenar a publish(). A 1.3 s el primer muerto
    // revive; que acepte una conexión prueba que se reintentó (su primer intento ya se dio por fallido).
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
    bool revivido = false;
    const auto inicio = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - inicio < 8s) {
        if (!revivido && std::chrono::steady_clock::now() - inicio >= 1300ms) {
            m1.revivir();
            revivido = true;
        }
        const auto t = std::chrono::steady_clock::now();
        (void)pub->publish(Img{n, 0, std::vector<std::uint8_t>(100, 1)});
        peor = std::max(peor, std::chrono::steady_clock::now() - t);
        ++n;
        if (revivido && m1.atender() >= 1) break;
        std::this_thread::sleep_for(10ms);
    }
    std::printf("  peor publish() con 3 destinos muertos: %.4f s (%u en %.2f s)\n", segundos(peor),
                static_cast<unsigned>(n), segundos(std::chrono::steady_clock::now() - inicio));
    CHECK(pub->subscribers() == 4);
    CHECK(m1.atender() >= 1);   // el destino que revivió se reintentó
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

void test_una_sola_espera_por_pasada() {
    // Varias respuestas del hub, cada una con una dirección nueva que no contesta, esperan en el socket:
    // el publish() que las lee espera una sola vez (50 ms), no una por respuesta.
    HubFalso h;
    DestinoMuerto m[5];
    auto pub = publisher_de(h);
    std::vector<std::string> lista;
    for (const auto& d : m) {
        lista.push_back(d.addr());
        h.lista(lista);
    }
    std::this_thread::sleep_for(100ms);   // todas ya están en el socket
    const auto t = std::chrono::steady_clock::now();
    (void)pub->publish(Img{1, 0, {1}});
    const auto primero = std::chrono::steady_clock::now() - t;
    std::printf("  el publish() que leyó 5 listas: %.4f s\n", segundos(primero));
    CHECK(pub->subscribers() == 5);
    CHECK(primero < 200ms);
}

/// Los avisos de Agmen, para contarlos.
struct Avisos {
    std::mutex mu;
    std::vector<std::string> lineas;
    Avisos() {
        agmen::log::set_sink([this](std::string_view l) {
            const std::lock_guard lock(mu);
            lineas.emplace_back(l);
        });
    }
    ~Avisos() { agmen::log::set_sink({}); }
    Avisos(const Avisos&) = delete;
    Avisos& operator=(const Avisos&) = delete;
    std::size_t con(const std::string& parte) {
        const std::lock_guard lock(mu);
        std::size_t n = 0;
        for (const auto& l : lineas) n += l.find(parte) != std::string::npos ? 1U : 0U;
        return n;
    }
};

const std::string kTodaviaConectando = "todavía conectando con";

/// Publica cada 20 ms durante `dura`; devuelve el peor publish().
std::chrono::steady_clock::duration publicar_durante(agmen::Publisher<Img>& pub, std::chrono::milliseconds dura) {
    std::chrono::steady_clock::duration peor{};
    std::uint32_t n = 0;
    const auto inicio = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - inicio < dura) {
        const auto t = std::chrono::steady_clock::now();
        (void)pub.publish(Img{n++, 0, {1}});
        peor = std::max(peor, std::chrono::steady_clock::now() - t);
        std::this_thread::sleep_for(20ms);
    }
    return peor;
}

void test_direccion_fija_a_otra_computadora_que_no_contesta() {
    // §13 C y D: crear espera hasta 1 s (una vez), publicar nunca; el aviso sale una vez. Sin ruta a esa
    // red (o si la máquina la rechaza en el acto) no hay espera, y a lo más un aviso (el de un reintento).
    const std::string ip = agmen::platform::env("AGMEN_PRUEBA_IP_MUDA").empty()
                               ? std::string("192.0.2.1")
                               : agmen::platform::env("AGMEN_PRUEBA_IP_MUDA");
    Avisos avisos;
    auto t = std::chrono::steady_clock::now();
    auto pub = std::make_unique<agmen::Publisher<Img>>(ip + ":9");
    const auto crear = std::chrono::steady_clock::now() - t;
    const auto peor = publicar_durante(*pub, 2500ms);
    t = std::chrono::steady_clock::now();
    pub.reset();   // close()
    const auto cierre = std::chrono::steady_clock::now() - t;
    const auto n_avisos = avisos.con(kTodaviaConectando);
    std::printf("  hacia %s:9: crear %.3f s, peor publish() %.4f s, close() %.4f s, %u aviso(s)%s\n", ip.c_str(),
                segundos(crear), segundos(peor), segundos(cierre), static_cast<unsigned>(n_avisos),
                crear < 500ms ? " (el connect terminó en el acto: sin espera)" : "");
    CHECK(crear < 1300ms);
    CHECK(peor < 200ms);
    CHECK(cierre < 200ms);
    if (crear >= 500ms) {
        CHECK(n_avisos == 1);
    } else {
        CHECK(n_avisos <= 1);
    }
}

void test_direccion_fija_local_donde_nadie_escucha() {
    // §13: una dirección de esta computadora sigue con 50 ms y, rechazada en el acto, callada. En Windows el
    // rechazo local tarda ~1 s (la conexión queda en curso): ahí se acepta un aviso.
    std::uint16_t puerto = 0;
    {
        auto srv = net::TcpListener::bind("127.0.0.1:0");
        puerto = srv.local_port();
    }
    Avisos avisos;
    const auto t = std::chrono::steady_clock::now();
    auto pub = std::make_unique<agmen::Publisher<Img>>("127.0.0.1:" + std::to_string(puerto));
    const auto crear = std::chrono::steady_clock::now() - t;
    const auto peor = publicar_durante(*pub, 200ms);
    pub.reset();
    const auto n_avisos = avisos.con(kTodaviaConectando);
    std::printf("  hacia 127.0.0.1:%u sin nadie: crear %.4f s, %u aviso(s)\n", static_cast<unsigned>(puerto),
                segundos(crear), static_cast<unsigned>(n_avisos));
    CHECK(crear < 200ms);
    CHECK(peor < 200ms);
#ifdef _WIN32
    CHECK(n_avisos <= 1);
#else
    CHECK(n_avisos == 0);
#endif
}

}  // namespace

int main() {
    test_un_megabyte_llega_entero();
    test_suscriptor_lento_recibe_lo_mas_nuevo();
    test_muchos_chicos_en_orden();
    test_publicar_no_espera_a_un_suscriptor_que_no_contesta();
    test_la_misma_lista_no_reintenta();
    test_una_sola_espera_por_pasada();
    test_direccion_fija_a_otra_computadora_que_no_contesta();
    test_direccion_fija_local_donde_nadie_escucha();
    if (g_failures == 0) std::printf("test_tcp: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
