// El transporte de los mensajes grandes (RFC 0008, D2 y D3): los tipos arrow
// viajan por TCP, un suscriptor lento recibe solo lo más nuevo y no frena a
// nadie. La misma prueba en Python: python/test_tcp.py.
#include <agmen/agmen.hpp>

#include <chrono>
#include <cstdio>
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

}  // namespace

int main() {
    test_un_megabyte_llega_entero();
    test_suscriptor_lento_recibe_lo_mas_nuevo();
    test_muchos_chicos_en_orden();
    if (g_failures == 0) std::printf("test_tcp: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
