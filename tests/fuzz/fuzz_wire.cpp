// Fuzzer del sobre y de los anuncios de esquema (src/wire.cpp).
//
// La entrada es un paquete tal como llega de la red (UDP o TCP). Lo único que
// se vale es aceptarlo o lanzar wire::WireError: cualquier otra excepción, un
// acceso fuera del buffer (ASan) o un comportamiento indefinido (UBSan) es un
// error del decodificador. Cómo correrlo: tests/fuzz/README.md.
#include <agmen/layout.hpp>
#include <agmen/msgs.hpp>
#include <agmen/wire.hpp>

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

namespace {

// Una propiedad que siempre debe cumplirse; si no, el fuzzer lo reporta.
#define REQUIRE(cond)                                                              \
    do {                                                                           \
        if (!(cond)) {                                                             \
            std::fprintf(stderr, "fuzz_wire: no se cumple %s (%s:%d)\n", #cond, __FILE__, __LINE__); \
            std::abort();                                                          \
        }                                                                          \
    } while (0)

void envelope(agmen::wire::ByteView packet) {
    namespace wire = agmen::wire;
    try {
        const auto [env, payload] = wire::Envelope::read_from(packet);
        // El payload es el final del paquete y mide lo que dice el sobre.
        REQUIRE(env.header_len >= wire::kEnvelopeLen && env.header_len <= packet.size());
        REQUIRE(payload.size() == env.payload_len);
        REQUIRE(payload.data() + payload.size() == packet.data() + packet.size());
        if (env.encoding != wire::Encoding::Announcement) return;
        const wire::Announcement a = wire::read_announcement(env, payload);
        // Un anuncio aceptado trae un esquema canónico: el lector de tiempo de
        // ejecución lo acepta y da exactamente el mismo texto y schema_id.
        const agmen::schema::Layout l(a.schema);
        REQUIRE(l.text() == a.schema);
        REQUIRE(l.schema_id() == env.schema_id);
        REQUIRE(wire::topic_id(a.topic) == env.topic_id);
    } catch (const wire::WireError&) {
    }
}

template <class M>
void typed(agmen::wire::ByteView packet) {
    try {
        const auto d = agmen::wire::decode<M>(packet);
        (void)d;
    } catch (const agmen::wire::WireError&) {
    }
}

// Si el paquete es un anuncio, la misma entrada con el schema_id y el
// topic_id que corresponden a su texto: sin esto el fuzzer casi nunca pasa la
// revisión de los hashes y no llega a leer el esquema anunciado.
void announcement_with_hashes(agmen::wire::ByteView packet) {
    namespace wire = agmen::wire;
    if (packet.size() < wire::kEnvelopeLen || packet[6] != static_cast<std::uint8_t>(wire::Encoding::Announcement)) return;
    const std::size_t header = std::size_t{packet[4]} | (std::size_t{packet[5]} << 8);
    if (header < wire::kEnvelopeLen || header > packet.size()) return;
    const std::string_view text(reinterpret_cast<const char*>(packet.data()) + header, packet.size() - header);
    const auto nl = text.find('\n');
    if (nl == std::string_view::npos) return;
    wire::Bytes fixed(packet.begin(), packet.end());
    const std::uint64_t sid = wire::fnv1a64(text.substr(nl + 1));
    const std::uint64_t tid = wire::topic_id(text.substr(0, nl));
    for (std::size_t k = 0; k < 8; ++k) {
        fixed[8 + k] = static_cast<std::uint8_t>(sid >> (8 * k));
        fixed[16 + k] = static_cast<std::uint8_t>(tid >> (8 * k));
    }
    envelope(fixed);
}

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    const agmen::wire::ByteView packet(data, size);
    envelope(packet);
    announcement_with_hashes(packet);
    // Los receptores tipados, uno compact y dos arrow (los de agmen/msgs.hpp).
    typed<agmen::wire::ImuSample>(packet);
    typed<agmen::msgs::Imagen>(packet);
    typed<agmen::msgs::ImagenComprimida>(packet);
    return 0;
}
