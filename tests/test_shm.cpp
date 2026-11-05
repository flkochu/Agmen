// Memoria compartida para los mensajes grandes (RFC 0009): los bytes del
// descriptor y de los avisos, los bloques, el pool, y el camino completo entre
// un Publisher y un Subscriber (préstamos, topes de D6, respaldo por TCP). La
// misma prueba en Python: python/test_shm.py.
#include <agmen/agmen.hpp>
#include <agmen/shm.hpp>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

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
namespace shm = agmen::shm;

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

std::vector<std::uint8_t> big(std::uint8_t seed = 0) {
    std::vector<std::uint8_t> out(1024 * 1000);   // 1 MB
    for (std::size_t i = 0; i < out.size(); ++i) out[i] = static_cast<std::uint8_t>(i + seed);
    return out;
}

bool is_shared(const agmen::RawSample& s) { return (s.envelope.flags & agmen::wire::kFlagShm) != 0; }

/// Publica hasta que un mensaje llegue por memoria compartida (la prueba tarda
/// un momento: mientras tanto llegan por TCP). Devuelve ese mensaje.
std::optional<agmen::RawSample> until_shared(agmen::Publisher<Img>& pub, agmen::Subscriber<Img>& sub,
                                             const std::vector<std::uint8_t>& data) {
    const auto end = std::chrono::steady_clock::now() + 3s;
    std::uint32_t k = 0;
    while (std::chrono::steady_clock::now() < end) {
        (void)pub.publish(Img{k++, 0, data});
        while (auto s = sub.receive_raw(20ms)) {
            if (is_shared(*s)) return s;
        }
    }
    return std::nullopt;
}

// --- bytes ------------------------------------------------------------------

void test_descriptor_y_avisos() {
    const shm::Descriptor d{"/agm0000002a0000beef0001", 1 << 20, 64, 921600, 7, 3};
    Bytes b;
    d.write_to(b);
    CHECK(b.size() == shm::kDescriptorLen);
    CHECK(std::memcmp(b.data(), "/agm0000002a0000beef0001", 24) == 0);
    CHECK(b[24] == 0 && b[31] == 0);   // el nombre va relleno con ceros
    std::uint64_t at40 = 0;
    std::memcpy(&at40, b.data() + 40, 8);
    CHECK(at40 == 64);
    std::uint32_t at64 = 0;
    std::memcpy(&at64, b.data() + 64, 4);
    CHECK(at64 == 3);
    const auto back = shm::Descriptor::read(b);
    CHECK(back.name == d.name && back.size == d.size && back.offset == d.offset && back.length == d.length &&
          back.generation == d.generation && back.slot == d.slot);

    Bytes bad = b;
    std::uint64_t huge = 1 << 21;   // la longitud no cabe en el bloque
    std::memcpy(bad.data() + 48, &huge, 8);
    bool threw = false;
    try {
        (void)shm::Descriptor::read(bad);
    } catch (const agmen::wire::WireError&) {
        threw = true;
    }
    CHECK(threw);

    Bytes r;
    shm::Record{shm::Op::Release, 5, 99}.write_to(r);
    CHECK(r.size() == shm::kRecordLen);
    CHECK(std::memcmp(r.data(), "AGB", 3) == 0 && r[3] == 4);
    const auto rr = shm::Record::read(r);
    CHECK(rr.op == shm::Op::Release && rr.slot == 5 && rr.generation == 99);
}

void test_sobre_con_shm_solo_si_se_pidio() {
    agmen::wire::Envelope env{.encoding = agmen::wire::Encoding::Arrow, .flags = agmen::wire::kFlagShm};
    env.payload_len = static_cast<std::uint32_t>(shm::kDescriptorLen);
    Bytes b;
    env.write_to(b);
    b.resize(b.size() + shm::kDescriptorLen, 0);
    bool threw = false;
    try {
        (void)agmen::wire::Envelope::read_from(b);
    } catch (const agmen::wire::WireError& e) {
        threw = e.kind() == agmen::wire::WireError::Kind::Unsupported;
    }
    CHECK(threw);   // un nodo que no la pidió no la confunde con datos
    const auto [got, payload] = agmen::wire::Envelope::read_from(b, true);
    CHECK((got.flags & agmen::wire::kFlagShm) != 0 && payload.size() == shm::kDescriptorLen);
}

// --- bloques y pool -----------------------------------------------------------

void test_bloque_se_ve_desde_otro_mapeo() {
    const std::string name = "/agmprueba" + std::to_string(agmen::wire::random_source_id() % 1000000);
    {
        auto w = shm::Segment::create(name, 65536);
        std::memcpy(w.data() + 100, "hola", 4);
        auto r = shm::Segment::open(name, 65536);
        CHECK(std::memcmp(r.data() + 100, "hola", 4) == 0);
        w.data()[100] = 'H';
        CHECK(r.data()[100] == 'H');   // la misma memoria, no una copia
        bool threw = false;
        try {
            (void)shm::Segment::create(name, 65536);   // ya existe
        } catch (const shm::ShmError&) {
            threw = true;
        }
        CHECK(threw);
    }
    bool threw = false;
    try {
        (void)shm::Segment::open("/agmnoexiste0000000000000", 4096);
    } catch (const shm::ShmError& e) {
        threw = std::string(e.what()).find("no pude abrir") != std::string::npos;
    }
    CHECK(threw);
}

void test_pool_reutiliza_y_crece() {
    shm::Pool pool(0x1234);
    const auto data = big();
    std::string why;
    const auto a = pool.write(1, data, why);
    CHECK(a.has_value());
    if (!a) return;
    CHECK(a->length == data.size() && a->offset == shm::kHeaderLen && a->generation == 1);
    const auto seg = shm::Segment::open(a->name, a->size);
    const auto h = shm::BlockHeader::read(seg.data());
    CHECK(h.owner == 0x1234 && h.generation == 1 && h.length == data.size());
    CHECK(std::memcmp(seg.data() + a->offset, data.data(), data.size()) == 0);

    const auto b = pool.write(2, data, why);   // nadie lo apartó: se reutiliza el mismo
    CHECK(b && b->slot == a->slot && pool.blocks() == 1);
    pool.hold(b->slot);
    const auto c = pool.write(3, data, why);   // apartado: hace falta otro
    CHECK(c && c->slot != b->slot && pool.blocks() == 2 && pool.busy() == 1);
    pool.release(b->slot);
    CHECK(pool.busy() == 0);

    const auto p = pool.probe(why);
    CHECK(p.has_value() && p->length == 0);
}

void test_pool_tiene_tope() {
    set_env("AGMEN_SHM_MB", "3");
    {
        shm::Pool pool(1);
        const auto data = big();
        std::string why;
        int ok = 0;
        for (std::uint64_t g = 0; g < 10; ++g) {
            const auto d = pool.write(g, data, why);
            if (!d) break;
            pool.hold(d->slot);   // nadie devuelve nada
            ++ok;
        }
        CHECK(ok >= 1 && ok <= 3);
        CHECK(why.find("AGMEN_SHM_MB") != std::string::npos);
    }
    unset_env("AGMEN_SHM_MB");
}

// --- Publisher → Subscriber ----------------------------------------------------

void test_un_megabyte_sin_copiar() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const auto data = big(3);
    const auto s = until_shared(pub, sub, data);
    CHECK(s.has_value());
    if (!s) return;
    CHECK(s->payload.shared());
    CHECK(s->envelope.payload_len == s->payload.size());
    const auto img = Img::decode_payload(s->payload);
    CHECK(img.data == data);
    CHECK(pub.shm_subscribers() == 1);
    CHECK(pub.shm_fallbacks() == 0);
}

void test_los_bloques_vuelven() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const auto data = big();
    CHECK(until_shared(pub, sub, data).has_value());
    // Uno a la vez, soltando cada uno: el publicador nunca se queda sin lugar.
    int shared = 0;
    for (std::uint32_t k = 0; k < 100; ++k) {
        (void)pub.publish(Img{k, 0, data});
        if (auto s = sub.receive_raw(1000ms)) shared += is_shared(*s) ? 1 : 0;
    }
    CHECK(shared == 100);
    CHECK(pub.shm_fallbacks() == 0);
    CHECK(pub.dropped() == 0);
}

void test_guardar_muchos_no_deja_a_nadie_sin_datos() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    CHECK(until_shared(pub, sub, big()).has_value());
    std::vector<agmen::RawSample> kept;
    for (std::uint32_t k = 0; k < 10; ++k) {
        (void)pub.publish(Img{k, 0, big(static_cast<std::uint8_t>(k))});
        if (auto s = sub.receive_raw(1000ms)) kept.push_back(std::move(*s));
    }
    CHECK(kept.size() == 10);   // los 10 llegan aunque ninguno se suelte
    std::size_t lent = 0;
    for (std::size_t k = 0; k < kept.size(); ++k) {
        lent += kept[k].payload.shared() ? 1U : 0U;
        const auto img = Img::decode_payload(kept[k].payload);
        CHECK(img.w == k && img.data == big(static_cast<std::uint8_t>(k)));
    }
    CHECK(lent == shm::kMaxLent);   // del 5.º en adelante se copian (D6)
}

void test_el_tope_de_prestados_tambien_es_en_bytes() {
    // Mensajes de 20 MB: caben 3 prestados en 64 MiB; el 4.º se copia (D6).
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const std::vector<std::uint8_t> huge(20u << 20, 5);
    CHECK(until_shared(pub, sub, big()).has_value());
    std::vector<agmen::RawSample> kept;
    for (std::uint32_t k = 0; k < 5; ++k) {
        (void)pub.publish(Img{k, 0, huge});
        if (auto s = sub.receive_raw(2000ms)) kept.push_back(std::move(*s));
    }
    CHECK(kept.size() == 5);
    std::size_t lent = 0;
    for (const auto& s : kept) lent += s.payload.shared() ? 1U : 0U;
    CHECK(lent == 3);
    CHECK(pub.shm_fallbacks() == 0);
}

void test_suscriptor_lento_recibe_lo_mas_nuevo() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const auto data = big();
    CHECK(until_shared(pub, sub, data).has_value());
    const auto before = pub.dropped();
    const auto t0 = std::chrono::steady_clock::now();
    for (std::uint32_t k = 0; k < 40; ++k) (void)pub.publish(Img{1000 + k, 0, data});   // nadie lee
    CHECK(std::chrono::steady_clock::now() - t0 < 1s);
    std::size_t got = 0;
    std::uint32_t last = 0;
    while (const auto s = sub.receive(300ms)) {
        ++got;
        last = s->message.w;
    }
    CHECK(last == 1039);   // el último siempre llega
    CHECK(got < 40);
    CHECK(got + (pub.dropped() - before) == 40);
    CHECK(pub.shm_fallbacks() == 0);
}

void test_chicos_en_linea_y_en_orden() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    CHECK(until_shared(pub, sub, big()).has_value());
    std::vector<std::uint32_t> got;
    std::size_t shared_ok = 0;
    auto take = [&](std::chrono::milliseconds wait) {
        while (const auto s = sub.receive_raw(wait)) {
            const auto img = Img::decode_payload(s->payload);
            got.push_back(img.w);
            shared_ok += is_shared(*s) == (img.w % 2 == 1) ? 1U : 0U;
            wait = 0ms;
        }
    };
    for (std::uint32_t k = 0; k < 60; ++k) {
        // uno chico (en línea por TCP) y uno grande (memoria compartida), alternados
        (void)pub.publish(Img{k, 0, std::vector<std::uint8_t>(k % 2 == 0 ? 100U : 200000U, 7)});
        take(20ms);
    }
    take(300ms);
    CHECK(got.size() == 60);
    CHECK(shared_ok == got.size());
    for (std::size_t i = 0; i < got.size(); ++i) CHECK(got[i] == i);   // en orden, sin adelantarse
}

void test_nada_se_adelanta_al_que_espera() {
    // Un suscriptor que no lee: los descriptores se detienen a los 8 sin
    // devolver (D6); lo que llegue después reemplaza al que espera, nunca se le
    // adelanta. Al leer, los números solo crecen.
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    CHECK(until_shared(pub, sub, big()).has_value());
    for (std::uint32_t k = 0; k < 60; ++k) {
        (void)pub.publish(Img{k, 0, std::vector<std::uint8_t>(k % 2 == 0 ? 100U : 200000U, 7)});
    }
    std::int64_t last = -1;
    bool increasing = true;
    while (const auto s = sub.receive_raw(300ms)) {
        const auto w = static_cast<std::int64_t>(Img::decode_payload(s->payload).w);
        increasing = increasing && w > last;
        last = w;
    }
    CHECK(increasing);
    CHECK(last == 59);
}

void test_agmen_shm_0_va_por_tcp() {
    set_env("AGMEN_SHM", "0");
    {
        agmen::Subscriber<Img> sub("127.0.0.1:0");
        agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
        const auto data = big();
        int got = 0;
        for (std::uint32_t k = 0; k < 20; ++k) {
            (void)pub.publish(Img{k, 0, data});
            if (auto s = sub.receive_raw(500ms)) {
                CHECK(!is_shared(*s) && !s->payload.shared());
                ++got;
            }
        }
        CHECK(got == 20);
        CHECK(pub.shm_subscribers() == 0);
    }
    unset_env("AGMEN_SHM");
}

void test_la_vista_sobrevive_al_suscriptor() {
    std::optional<agmen::RawSample> kept;
    std::vector<std::uint8_t> data = big(9);
    auto pub_port = std::uint16_t{0};
    {
        agmen::Subscriber<Img> sub("127.0.0.1:0");
        pub_port = sub.local_port();
        agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(pub_port), "/img");
        kept = until_shared(pub, sub, data);
        CHECK(kept.has_value());
        // El Subscriber se cierra con el mensaje prestado; el publicador sigue.
    }
    if (kept) {
        const auto img = Img::decode_payload(kept->payload);
        CHECK(img.data == data);   // la vista sigue siendo válida (el bloque no se borró debajo)
    }
}

void test_tipado_por_memoria_compartida() {
    agmen::Subscriber<Img> sub("127.0.0.1:0");
    agmen::Publisher<Img> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/img");
    const auto data = big(1);
    CHECK(until_shared(pub, sub, data).has_value());
    (void)pub.publish(Img{640, 480, data});
    const auto s = sub.receive(1000ms);
    CHECK(s.has_value());
    if (s) {
        CHECK((s->envelope.flags & agmen::wire::kFlagShm) != 0);
        CHECK(s->message.w == 640 && s->message.h == 480 && s->message.data == data);
    }
}

}  // namespace

int main() {
    unset_env("AGMEN_SHM");
    test_descriptor_y_avisos();
    test_sobre_con_shm_solo_si_se_pidio();
    test_bloque_se_ve_desde_otro_mapeo();
    test_pool_reutiliza_y_crece();
    test_pool_tiene_tope();
    test_un_megabyte_sin_copiar();
    test_los_bloques_vuelven();
    test_guardar_muchos_no_deja_a_nadie_sin_datos();
    test_el_tope_de_prestados_tambien_es_en_bytes();
    test_suscriptor_lento_recibe_lo_mas_nuevo();
    test_chicos_en_linea_y_en_orden();
    test_nada_se_adelanta_al_que_espera();
    test_agmen_shm_0_va_por_tcp();
    test_la_vista_sobrevive_al_suscriptor();
    test_tipado_por_memoria_compartida();
    if (g_failures == 0) std::printf("test_shm: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
