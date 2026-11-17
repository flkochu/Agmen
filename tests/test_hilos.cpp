// Los objetos del núcleo desde varios hilos a la vez (PR "hilos", fase A: auditoría).
//
// Garantía que se prueba (especificación §1): publicar×publicar sobre el mismo Publisher saca cada
// mensaje entero y una sola vez; recibir×recibir sobre el mismo Subscriber (turnados por un candado, como hace el
// binding de Python: receive_raw() del núcleo no es para dos hilos a la vez) entrega cada mensaje a un solo hilo; las propiedades de lectura (subscribers(), seq(), dropped(), announced(), native_handles()…)
// se pueden leer mientras otro hilo publica o recibe, sin carrera de datos. Esa última parte la dice
// ThreadSanitizer: compila con -DAGMEN_TSAN=ON y corre ctest. Sin TSan, la prueba comprueba lo que se
// ve desde fuera (nada se duplica, nada llega a medias, nada se cuelga).
//
// El núcleo no tiene close(): cerrar es destruir el objeto (Python lo hace en python/src/core.cpp).
// Por eso publicar×cerrar y recibir×cerrar se prueban en python/test_hilos.py y, con TSan, sobre
// agmen._core. Lo que no se simula aquí: el paso a la red (hace falta un hub; ver tests/test_red.cpp).
//
// Ninguna espera es un tiempo fijo: cada hilo termina por una bandera, y un vigilante aborta el
// proceso con un mensaje si algo se cuelga.
#include <agmen/agmen.hpp>
#include <agmen/wire.hpp>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

int g_failures = 0;
std::mutex g_failures_mu;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::lock_guard<std::mutex> lk_(g_failures_mu);                         \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

using namespace std::chrono_literals;
namespace wire = agmen::wire;
using wire::Bytes;
using wire::ByteView;

// Un tipo compact (por UDP) y uno arrow (por TCP), escritos a mano (lo que genera agmen gen).
struct Chico {
    static constexpr std::string_view kName = "ChicoHilos";
    static constexpr std::string_view kSchema = "ChicoHilos{u32 a;u32 b}";
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
    static constexpr std::string_view kName = "GrandeHilos";
    static constexpr std::string_view kSchema = "GrandeHilos{u32 w;u32 n;bytes data}";
    std::uint32_t w = 0, n = 0;
    std::vector<std::uint8_t> data;

    static const agmen::arrow::Codec& codec() {
        static const agmen::schema::Layout l(kSchema);
        static const agmen::arrow::Codec c(l);
        return c;
    }
    void encode_payload(Bytes& out) const {
        using V = agmen::arrow::Value;
        codec().encode(V::structure({V::uinteger(w), V::uinteger(n), V::blob(data)}), out);
    }
    static Grande decode_payload(ByteView p) {
        const auto v = codec().decode(p);
        Grande m;
        m.w = v.items[0].as<std::uint32_t>();
        m.n = v.items[1].as<std::uint32_t>();
        m.data.assign(v.items[2].bytes().begin(), v.items[2].bytes().end());
        return m;
    }
};
static_assert(wire::is_arrow_v<Grande>);
static_assert(!wire::is_arrow_v<Chico>);

/// Aborta con un mensaje si el proceso tarda más de lo razonable (un cuelgue es una falla, no un tiempo
/// de espera de ctest sin explicación).
class Vigilante {
public:
    explicit Vigilante(std::chrono::seconds tope) : listo_(std::make_shared<std::atomic<bool>>(false)) {
        // La bandera es compartida: el hilo (separado) puede sobrevivir a este objeto sin tocar memoria ajena.
        std::thread([listo = listo_, tope] {
            const auto fin = std::chrono::steady_clock::now() + tope;
            while (std::chrono::steady_clock::now() < fin) {
                if (listo->load()) return;
                std::this_thread::sleep_for(20ms);
            }
            std::fprintf(stderr, "  FALLA: la prueba se colgó (más de %lld s)\n", static_cast<long long>(tope.count()));
            std::fflush(stderr);
            std::_Exit(2);
        }).detach();
    }
    ~Vigilante() { *listo_ = true; }
    Vigilante(const Vigilante&) = delete;
    Vigilante& operator=(const Vigilante&) = delete;

private:
    std::shared_ptr<std::atomic<bool>> listo_;
};

constexpr int kHilosPub = 4;
constexpr std::uint32_t kPorHilo = 250;

/// Un mensaje de `M` que identifica de dónde sale: (hilo, número).
template <class M>
M make(std::uint32_t hilo, std::uint32_t k);
template <>
Chico make<Chico>(std::uint32_t hilo, std::uint32_t k) {
    return Chico{hilo, k};
}
template <>
Grande make<Grande>(std::uint32_t hilo, std::uint32_t k) {
    Grande g;
    g.w = hilo;
    g.n = k;
    g.data.assign(2000, static_cast<std::uint8_t>(hilo * 16U + (k & 15U)));
    return g;
}
std::pair<std::uint32_t, std::uint32_t> ident(const Chico& m) { return {m.a, m.b}; }
std::pair<std::uint32_t, std::uint32_t> ident(const Grande& m) {
    // Un mensaje a medias o mezclado de dos hilos no cuadra con su relleno.
    const auto fill = static_cast<std::uint8_t>(m.w * 16U + (m.n & 15U));
    CHECK(m.data.size() == 2000);
    for (auto v : m.data) {
        if (v != fill) {
            CHECK(v == fill);
            break;
        }
    }
    return {m.w, m.n};
}

/// Lee todo lo que un hilo de monitoreo puede leer de un Publisher.
void leer_publicador(const agmen::PublisherCore& p, std::uint64_t& basura) {
    basura += p.seq() + p.source_id() + p.subscribers() + p.dropped() + p.shm_subscribers() + p.shm_fallbacks() +
              p.shm_bytes() + p.unreachable() + p.rejected() + p.net_port() + p.topic().size();
}

/// Lo mismo de un Subscriber, sin tocar lo que cambia con receive_raw() de forma que no sea API pública.
void leer_suscriptor(const agmen::SubscriberCore& s, std::uint64_t& basura) {
    basura += s.shm_mapped() + s.local_port() + s.tcp_port() + s.rejected() + s.where().size();
    basura += s.announced().size();
    basura += s.native_handles().size();
    const auto dir = s.listen_address();
    basura += dir.port + dir.tcp_port + dir.ip.size();
    basura += s.listen_ip().size();
    basura += static_cast<std::uint64_t>(s.native_handle() != 0);
}

/// publicar×publicar sobre el mismo Publisher (más lecturas y latidos desde otros hilos) y, a la vez, un
/// hilo que recibe: cada mensaje llega entero, una vez, con un seq distinto.
template <class M>
void test_publicar_publicar(const char* nombre) {
    std::printf("  publicar×publicar (%s)\n", nombre);
    const auto info = agmen::TypeInfo::of<M>();
    agmen::SubscriberCore sub("127.0.0.1:0", info);
    agmen::PublisherCore pub("127.0.0.1:" + std::to_string(sub.local_port()), info, "/hilos/pp");

    std::atomic<int> publicando{kHilosPub};
    std::atomic<bool> fin_lecturas{false};
    std::atomic<std::uint64_t> total_ok{0};
    std::vector<std::thread> hilos;

    std::vector<std::uint64_t> seqs;
    std::set<std::pair<std::uint32_t, std::uint32_t>> vistos;
    bool duplicado = false;
    std::thread receptor([&] {
        int vacios = 0;
        while (vacios < 20) {   // 20 × 50 ms sin nada, y ya no hay quien publique
            auto raw = sub.receive_raw(50ms);
            if (!raw) {
                if (publicando.load() == 0) ++vacios;
                continue;
            }
            vacios = 0;
            seqs.push_back(raw->envelope.seq);
            const auto id = ident(M::decode_payload(raw->payload.view()));
            if (!vistos.insert(id).second) duplicado = true;
        }
    });

    for (std::uint32_t h = 0; h < kHilosPub; ++h) {
        hilos.emplace_back([&, h] {
            for (std::uint32_t k = 0; k < kPorHilo; ++k) {
                Bytes payload;
                make<M>(h, k).encode_payload(payload);
                pub.publish_payload(payload, agmen::wire::now_ns());
                total_ok.fetch_add(1);
                if ((k & 31U) == 0) std::this_thread::yield();
            }
            publicando.fetch_sub(1);
        });
    }
    std::vector<std::thread> lectores;
    for (int i = 0; i < 2; ++i) {
        lectores.emplace_back([&] {
            std::uint64_t basura = 0;
            while (!fin_lecturas.load()) {
                leer_publicador(pub, basura);
                std::this_thread::yield();
            }
        });
    }
    lectores.emplace_back([&] {   // el latido y el anuncio los da otro hilo (Python: el hilo de latidos)
        while (!fin_lecturas.load()) {
            pub.heartbeat();
            pub.announce();
            std::this_thread::sleep_for(1ms);
        }
    });
    for (auto& t : hilos) t.join();
    receptor.join();
    fin_lecturas = true;
    for (auto& t : lectores) t.join();

    CHECK(pub.seq() == static_cast<std::uint64_t>(kHilosPub) * kPorHilo);
    CHECK(!duplicado);
    std::set<std::uint64_t> unicos(seqs.begin(), seqs.end());
    CHECK(unicos.size() == seqs.size());   // ningún seq repetido
    for (auto s : unicos) CHECK(s < static_cast<std::uint64_t>(kHilosPub) * kPorHilo);
    CHECK(!seqs.empty());
}

/// recibir×recibir sobre el mismo Subscriber, con lectores de propiedades y latidos desde otros hilos.
template <class M>
void test_recibir_recibir(const char* nombre) {
    std::printf("  recibir×recibir y lecturas (%s)\n", nombre);
    const auto info = agmen::TypeInfo::of<M>();
    agmen::SubscriberCore sub("127.0.0.1:0", info);
    agmen::PublisherCore pub("127.0.0.1:" + std::to_string(sub.local_port()), info, "/hilos/rr");

    constexpr int kReceptores = 3;
    constexpr std::uint32_t kMensajes = 400;
    std::atomic<bool> publicado{false};
    std::atomic<bool> fin_lecturas{false};

    std::mutex mu;
    std::mutex turno_rx;
    std::set<std::pair<std::uint32_t, std::uint32_t>> vistos;
    std::set<std::uint64_t> seqs;
    std::atomic<int> repetidos{0};
    std::atomic<int> recibidos{0};

    std::vector<std::thread> receptores;
    for (int i = 0; i < kReceptores; ++i) {
        receptores.emplace_back([&] {
            int vacios = 0;
            while (vacios < 20) {
                std::optional<agmen::RawSample> raw;
                {
                    // El turno de recibir: receive_raw() no es para dos hilos a la vez (el Subscriber de
                    // Python los turna con un candado, python/src/core.cpp). Lo demás se lee sin él.
                    std::lock_guard turno(turno_rx);
                    raw = sub.receive_raw(50ms);
                }
                if (!raw) {
                    if (publicado.load()) ++vacios;
                    continue;
                }
                vacios = 0;
                recibidos.fetch_add(1);
                const auto id = ident(M::decode_payload(raw->payload.view()));
                std::lock_guard lock(mu);
                if (!vistos.insert(id).second) repetidos.fetch_add(1);
                if (!seqs.insert(raw->envelope.seq).second) repetidos.fetch_add(1);
            }
        });
    }
    std::vector<std::thread> lectores;
    for (int i = 0; i < 2; ++i) {
        lectores.emplace_back([&] {
            std::uint64_t basura = 0;
            while (!fin_lecturas.load()) {
                leer_suscriptor(sub, basura);
                std::this_thread::yield();
            }
        });
    }
    lectores.emplace_back([&] {   // el latido lo da otro hilo mientras uno espera (documentado como seguro)
        while (!fin_lecturas.load()) {
            sub.heartbeat();
            std::this_thread::sleep_for(1ms);
        }
    });

    for (std::uint32_t k = 0; k < kMensajes; ++k) {
        Bytes payload;
        make<M>(0, k).encode_payload(payload);
        pub.publish_payload(payload, agmen::wire::now_ns());
        if ((k & 15U) == 0) std::this_thread::sleep_for(1ms);   // que el receptor del núcleo no se quede atrás
    }
    publicado = true;
    for (auto& t : receptores) t.join();
    fin_lecturas = true;
    for (auto& t : lectores) t.join();

    CHECK(repetidos.load() == 0);
    CHECK(recibidos.load() > 0);
    if constexpr (wire::is_arrow_v<M>) CHECK(recibidos.load() == static_cast<int>(kMensajes));   // TCP: sin pérdidas
}

/// publicar×publicar y recibir×recibir a la vez, cruzados: varios publicadores sobre un mismo Subscriber.
void test_cruzado() {
    std::printf("  dos Publisher y varios hilos en cada lado, sobre un mismo Subscriber\n");
    const auto info = agmen::TypeInfo::of<Chico>();
    agmen::SubscriberCore sub("127.0.0.1:0", info);
    const std::string dir = "127.0.0.1:" + std::to_string(sub.local_port());
    agmen::PublisherCore p1(dir, info, "/hilos/x1");
    agmen::PublisherCore p2(dir, info, "/hilos/x2");
    std::atomic<int> publicando{4};
    std::mutex mu;
    std::mutex turno_rx;
    std::set<std::pair<std::uint64_t, std::uint64_t>> vistos;   // (source_id, seq)
    std::atomic<int> repetidos{0};
    std::vector<std::thread> hilos;
    for (int i = 0; i < 2; ++i) {
        hilos.emplace_back([&] {
            int vacios = 0;
            while (vacios < 20) {
                std::optional<agmen::RawSample> raw;
                {
                    std::lock_guard turno(turno_rx);
                    raw = sub.receive_raw(50ms);
                }
                if (!raw) {
                    if (publicando.load() == 0) ++vacios;
                    continue;
                }
                vacios = 0;
                std::lock_guard lock(mu);
                if (!vistos.insert({raw->envelope.source_id, raw->envelope.seq}).second) repetidos.fetch_add(1);
            }
        });
    }
    for (int i = 0; i < 4; ++i) {
        hilos.emplace_back([&, i] {
            auto& p = (i % 2 == 0) ? p1 : p2;
            for (std::uint32_t k = 0; k < 200; ++k) {
                Bytes payload;
                make<Chico>(static_cast<std::uint32_t>(i), k).encode_payload(payload);
                p.publish_payload(payload, agmen::wire::now_ns());
                if ((k & 31U) == 0) std::this_thread::yield();
            }
            publicando.fetch_sub(1);
        });
    }
    for (auto& t : hilos) t.join();
    CHECK(repetidos.load() == 0);
    CHECK(p1.seq() == 400);
    CHECK(p2.seq() == 400);
}

}  // namespace

// Sin argumentos corre todo; con un nombre (pp_compact, pp_arrow, rr_compact, rr_arrow, cruzado) solo ese
// caso: así un informe de ThreadSanitizer se atribuye a un caso.
int main(int argc, char** argv) {
    const Vigilante vigilante(120s);
    const std::string solo = argc > 1 ? argv[1] : "";
    const auto quiere = [&](const char* n) { return solo.empty() || solo == n; };
    std::printf("test_hilos\n");
    if (quiere("pp_compact")) test_publicar_publicar<Chico>("compact, UDP");
    if (quiere("pp_arrow")) test_publicar_publicar<Grande>("arrow, TCP");
    if (quiere("rr_compact")) test_recibir_recibir<Chico>("compact, UDP");
    if (quiere("rr_arrow")) test_recibir_recibir<Grande>("arrow, TCP");
    if (quiere("cruzado")) test_cruzado();
    if (g_failures != 0) {
        std::fprintf(stderr, "test_hilos: %d fallas\n", g_failures);
        return 1;
    }
    std::printf("test_hilos: ok\n");
    return 0;
}
