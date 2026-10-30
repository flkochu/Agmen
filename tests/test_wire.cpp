// Pruebas del formato de cable. Se corren con `ctest`.
//
// Sin frameworks externos a propósito: un mini-arnés de 20 líneas es
// suficiente y no agrega dependencias. Si alguna falla, rompiste la
// compatibilidad con Python u otros lenguajes.

#include <motio/wire.hpp>

#include <cstdio>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                      \
    do {                                                                                 \
        if (!(cond)) {                                                                   \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            ++g_failures;                                                                \
        }                                                                                \
    } while (0)

template <class F>
void expect_error(F&& f, motio::wire::WireError::Kind kind, std::string_view must_contain = {}) {
    try {
        f();
        std::fprintf(stderr, "  FALLA: se esperaba WireError y no se lanzó\n");
        ++g_failures;
    } catch (const motio::wire::WireError& e) {
        CHECK(e.kind() == kind);
        CHECK(std::string_view(e.what()).find(must_contain) != std::string_view::npos);
    }
}

using namespace motio::wire;

ImuSample sample() {
    return ImuSample{.accel = {0.1f, -9.81f, 0.0f}, .gyro = {0.01f, 0.02f, -0.03f}};
}

// --- El hash se calcula en compilación y debe coincidir con Python. ---
static_assert(fnv1a64("") == 0xcbf29ce484222325ULL);
static_assert(fnv1a64("a") == 0xaf63dc4c8601ec8cULL);
static_assert(type_id_v<ImuSample> == 0x14626bee23e54e09ULL);

void ida_y_vuelta() {
    const auto bytes = encode(sample(), 42, 123456789);
    CHECK(bytes.size() == kHeaderLen + ImuSample::kPayloadLen);
    const auto [h, m] = decode<ImuSample>(bytes);
    CHECK(h.seq == 42);
    CHECK(h.stamp_ns == 123456789);
    CHECK(m == sample());
}

void layout_del_header_es_estable() {
    // Bytes exactos esperados: si esto cambia, se rompe la compatibilidad.
    const auto b = encode(sample(), 1, 2);
    CHECK(b[0] == 'M' && b[1] == 'O' && b[2] == 'T');
    CHECK(b[3] == 0);
    CHECK(get_le<std::uint64_t>(b, 4) == type_id_v<ImuSample>);
    CHECK(b[4] == 0x09 && b[11] == 0x14);  // little-endian de verdad
    CHECK(get_le<std::uint64_t>(b, 12) == 1);
    CHECK(get_le<std::uint64_t>(b, 20) == 2);
    CHECK(get_le<std::uint32_t>(b, 28) == 24);
    // -9.81f en little-endian: 0xC11CF5C3 -> C3 F5 1C C1
    CHECK(b[36] == 0xC3 && b[37] == 0xF5 && b[38] == 0x1C && b[39] == 0xC1);
}

void rechaza_magic_invalido() {
    auto b = encode(sample(), 0, 0);
    b[0] = 'X';
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::BadMagic, "Motio");
}

void rechaza_tipo_incorrecto() {
    auto b = encode(sample(), 0, 0);
    b[4] ^= 0xFF;  // corrompemos el type_id
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::TypeMismatch,
                 "TIPO INCOMPATIBLE");
}

void rechaza_paquete_truncado() {
    const auto b = encode(sample(), 0, 0);
    expect_error([&] { (void)decode<ImuSample>(ByteView(b).first(10)); },
                 WireError::Kind::TooShort);
    expect_error([&] { (void)decode<ImuSample>(ByteView(b).first(40)); },
                 WireError::Kind::PayloadLenMismatch);
}

void rechaza_version_desconocida() {
    auto b = encode(sample(), 0, 0);
    b[3] = 7;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::UnsupportedVersion);
}

}  // namespace

int main() {
    const std::vector<std::pair<const char*, std::function<void()>>> tests = {
        {"ida_y_vuelta", ida_y_vuelta},
        {"layout_del_header_es_estable", layout_del_header_es_estable},
        {"rechaza_magic_invalido", rechaza_magic_invalido},
        {"rechaza_tipo_incorrecto", rechaza_tipo_incorrecto},
        {"rechaza_paquete_truncado", rechaza_paquete_truncado},
        {"rechaza_version_desconocida", rechaza_version_desconocida},
    };
    for (const auto& [name, fn] : tests) {
        const int before = g_failures;
        fn();
        std::printf("%s %s\n", g_failures == before ? "ok   " : "FALLA", name);
    }
    std::printf("\n%zu pruebas, %d fallas\n", tests.size(), g_failures);
    return g_failures == 0 ? 0 : 1;
}
