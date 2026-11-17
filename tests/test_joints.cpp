// JointState<N> y JointCommand<N> de <agmen/joints.hpp> (RFC 0013 §3.4, D5 = f, D7 = a):
//   - contra los vectores compartidos (tests/vectors/joints.txt), los mismos que revisan Python
//     (python/test_joints.py, con los dos núcleos) y JavaScript (agmen.js): texto canónico,
//     schema_id, tamaño y bytes, con N = 2 y N = 12;
//   - el valor por defecto (los bits de NaN: 0x7FC00000) y los ceros del comando;
//   - es EL MISMO tipo en la red que el que genera `agmen gen` desde un .agmen normal
//     (tests/joints_usuario.agmen): mismo texto, mismo schema_id y mismos bytes;
//   - código general sobre N: una función template <std::size_t N> usada con 2 y con 12;
//   - wire::encode / wire::decode y un payload de otro tamaño (error claro).
// Con NaN un mensaje no es igual a sí mismo: aquí se comparan bytes o se usa isnan (y la
// plantilla no trae operator==, a propósito).
#include "joints_usuario.hpp"   // generado por agmen_add_messages(joints_usuario_msgs tests/joints_usuario.agmen)

#include <agmen/joints.hpp>

#include <bit>
#include <cmath>
#include <concepts>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <limits>
#include <map>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#ifndef AGMEN_JOINTS_FILE
#error "CMake debe definir AGMEN_JOINTS_FILE"
#endif

int joints_solo_check();   // test_joints_solo.cpp: una unidad de traducción que solo incluye <agmen/joints.hpp>

namespace {

namespace m = agmen::msgs;
namespace w = agmen::wire;

int g_failures = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

// La plantilla no trae operator== (RFC 0013, D5): que nadie lo agregue sin querer.
template <class T>
concept Comparable = requires(const T& a, const T& b) { a == b; };
static_assert(!Comparable<m::JointState<12>> && !Comparable<m::JointCommand<2>>);

// Mensajes de Agmen como cualquier tipo generado, con 1, 2, 12 y el máximo que cabe en UDP.
static_assert(w::Message<m::JointState<1>> && w::Message<m::JointState<2>> && w::Message<m::JointState<5453>>);
static_assert(w::Message<m::JointCommand<1>> && w::Message<m::JointCommand<12>> &&
              w::Message<m::JointCommand<3272>>);
static_assert(w::compact_size_v<m::JointState<5453>> == 65436 && w::compact_size_v<m::JointCommand<3272>> == 65440);
static_assert(!w::is_arrow_v<m::JointState<2>> && w::encoding_v<m::JointCommand<12>> == w::Encoding::Compact);
static_assert(m::JointState<1>::kSchema == "JointState{f32[1] position;f32[1] velocity;f32[1] effort}");
static_assert(m::JointState<10>::kSchema == "JointState{f32[10] position;f32[10] velocity;f32[10] effort}");
static_assert(m::JointState<5453>::kSchema ==
              "JointState{f32[5453] position;f32[5453] velocity;f32[5453] effort}");
static_assert(m::JointState<2>::kName == "JointState" && m::JointCommand<2>::kName == "JointCommand");
static_assert(m::JointState<2>::kPayloadLen == 24 && m::JointCommand<12>::kPayloadLen == 240);
// Con N distinto el tipo es otro (el hub no deja mezclar 2 y 12 en un topic).
static_assert(w::schema_id_v<m::JointState<2>> != w::schema_id_v<m::JointState<12>>);
// El .agmen normal de hoy y la plantilla: el mismo tipo en la red.
static_assert(m::JointState<12>::kSchema == pruebas_joints::JointState::kSchema);
static_assert(w::schema_id_v<m::JointState<12>> == w::schema_id_v<pruebas_joints::JointState>);
static_assert(w::schema_id_v<m::JointCommand<2>> == w::schema_id_v<pruebas_joints::JointCommand>);
static_assert(!std::same_as<m::JointState<12>, pruebas_joints::JointState>);   // son tipos de C++ distintos

std::uint32_t bits(float f) { return std::bit_cast<std::uint32_t>(f); }

std::vector<std::string> columns(const std::string& line) {
    std::vector<std::string> cols;
    std::stringstream ss(line);
    std::string col;
    while (std::getline(ss, col, '\t')) cols.push_back(col);
    return cols;
}

struct Tipo {
    std::string text, id;
    std::size_t size = 0;
    std::map<std::string, std::string> valores;   // etiqueta → payload_hex
};

/// (nombre, N) → lo que dice el vector.
std::map<std::pair<std::string, std::size_t>, Tipo> load_vectors() {
    std::map<std::pair<std::string, std::size_t>, Tipo> out;
    std::ifstream in(AGMEN_JOINTS_FILE);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();   // por si git lo cambió a CRLF
        const auto cols = columns(line);
        if (cols.empty()) continue;
        // tipo   nombre  N  canónico  schema_id  tamaño
        // valor  nombre  N  etiqueta  valores    payload_hex
        if (cols[0] == "tipo") {
            Tipo& t = out[{cols.at(1), std::stoul(cols.at(2))}];
            t.text = cols.at(3);
            t.id = cols.at(4);
            t.size = static_cast<std::size_t>(std::stoul(cols.at(5)));
        } else if (cols[0] == "valor") {
            out[{cols.at(1), std::stoul(cols.at(2))}].valores[cols.at(3)] = cols.at(5);
        }
    }
    return out;
}

w::Bytes from_hex(const std::string& h) {
    w::Bytes b;
    for (std::size_t i = 0; i + 1 < h.size(); i += 2) {
        b.push_back(static_cast<std::uint8_t>(std::stoul(h.substr(i, 2), nullptr, 16)));
    }
    return b;
}

std::string hex(std::uint64_t v) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "0x%016llx", static_cast<unsigned long long>(v));
    return buf;
}

template <class M>
w::Bytes bytes_of(const M& v) {
    w::Bytes out;
    v.encode_payload(out);
    return out;
}

/// Revisa M contra su vector: texto, schema_id, tamaño, el valor por defecto de C++ y cada valor
/// (decodificar y volver a codificar da los mismos bytes, NaN incluidos; y por wire::encode/decode).
template <class M, std::size_t N>
void check_vectors(const std::map<std::pair<std::string, std::size_t>, Tipo>& vectors) {
    const std::string name(M::kName);
    const auto it = vectors.find({name, N});
    if (it == vectors.end()) {
        std::fprintf(stderr, "  FALLA: no hay vector para %s<%zu>\n", name.c_str(), N);
        ++g_failures;
        return;
    }
    const Tipo& v = it->second;
    CHECK(w::schema_text<M>() == v.text);
    CHECK(hex(w::schema_id_v<M>) == v.id);
    CHECK(M::kPayloadLen == v.size);
    CHECK(v.valores.count("por_defecto") == 1 && v.valores.count("distintos") == 1);
    for (const auto& [etiqueta, payload_hex] : v.valores) {
        const auto payload = from_hex(payload_hex);
        CHECK(payload.size() == M::kPayloadLen);
        const M msg = M::decode_payload(payload);
        if (bytes_of(msg) != payload) {
            std::fprintf(stderr, "  FALLA %s<%zu> %s: volver a codificar no da los mismos bytes\n", name.c_str(), N,
                         etiqueta.c_str());
            ++g_failures;
        }
        // De punta a punta por el sobre: el t_data y los bytes llegan intactos.
        const auto packet = w::encode(msg, 7, 5'000'000'123LL);
        const auto back = w::decode<M>(packet);
        CHECK(back.envelope.schema_id == w::schema_id_v<M>);
        CHECK(back.envelope.t_data == 5'000'000'123LL);
        CHECK(bytes_of(back.message) == payload);
    }
    if (bytes_of(M{}) != from_hex(v.valores.at("por_defecto"))) {
        std::fprintf(stderr, "  FALLA %s<%zu>: el valor por defecto de C++ no da los bytes del vector\n",
                     name.c_str(), N);
        ++g_failures;
    }
    std::printf("  ok %-12s N=%-3zu %3zu bytes  %s  (%zu valores)\n", name.c_str(), N, M::kPayloadLen, v.id.c_str(),
                v.valores.size());
}

/// "distintos" del vector: el campo k, articulación i → ±(100·(k+1) + i + 0.125). Decodificado,
/// cada número cae en su lugar (un orden cambiado, de articulaciones o de campos, se notaría).
template <std::size_t N>
void check_order(const std::map<std::pair<std::string, std::size_t>, Tipo>& vectors) {
    const auto expect = [](std::size_t k, std::size_t i) {
        const float v = static_cast<float>(100 * (k + 1) + i) + 0.125f;
        return i % 2 == 0 ? v : -v;
    };
    const auto s = m::JointState<N>::decode_payload(from_hex(vectors.at({"JointState", N}).valores.at("distintos")));
    const auto c = m::JointCommand<N>::decode_payload(
        from_hex(vectors.at({"JointCommand", N}).valores.at("distintos")));
    for (std::size_t i = 0; i < N; ++i) {
        CHECK(s.position[i] == expect(0, i) && s.velocity[i] == expect(1, i) && s.effort[i] == expect(2, i));
        CHECK(c.position[i] == expect(0, i) && c.velocity[i] == expect(1, i) && c.effort[i] == expect(2, i));
        CHECK(c.kp[i] == expect(3, i) && c.kd[i] == expect(4, i));
    }
}

/// Los valores por defecto, bit por bit: NaN = 0x7FC00000 (el NaN quieto de Agmen), y en el comando
/// velocity = effort = +0.0 (los ceros no son el valor por defecto de las ganancias).
template <std::size_t N>
void check_defaults() {
    const m::JointState<N> s;
    const m::JointCommand<N> c;
    for (std::size_t i = 0; i < N; ++i) {
        CHECK(bits(s.position[i]) == 0x7FC00000U && bits(s.velocity[i]) == 0x7FC00000U &&
              bits(s.effort[i]) == 0x7FC00000U);
        CHECK(bits(c.position[i]) == 0x7FC00000U && bits(c.kp[i]) == 0x7FC00000U && bits(c.kd[i]) == 0x7FC00000U);
        CHECK(bits(c.velocity[i]) == 0U && bits(c.effort[i]) == 0U);
        CHECK(std::isnan(s.position[i]) && !(s.position[i] == s.position[i]));   // NaN nunca es igual a NaN
    }
}

// --- Código general sobre N (RFC 0013 §3.4): sirve para 2, 12 o 29 articulaciones ---

/// Frena: sin objetivo de posición, velocidad 0, sin par extra, con las ganancias del grupo.
template <std::size_t N>
void frenar(m::JointCommand<N>& cmd) {
    constexpr float nan = std::numeric_limits<float>::quiet_NaN();
    cmd.position.fill(nan);
    cmd.velocity.fill(0.0f);
    cmd.effort.fill(0.0f);
    cmd.kp.fill(nan);
    cmd.kd.fill(2.0f);   // un kd propio, para ver que se toca cada arreglo
}

/// Cuántas articulaciones miden su posición (las demás vienen en NaN).
template <std::size_t N>
std::size_t medidas(const m::JointState<N>& s) {
    std::size_t k = 0;
    for (float q : s.position) {
        if (!std::isnan(q)) ++k;
    }
    return k;
}

template <std::size_t N>
void check_general() {
    m::JointCommand<N> cmd;
    for (std::size_t i = 0; i < N; ++i) {
        cmd.position[i] = 1.0f;
        cmd.velocity[i] = 3.0f;
        cmd.effort[i] = 4.0f;
        cmd.kp[i] = 5.0f;
    }
    frenar(cmd);
    const auto b = bytes_of(cmd);
    CHECK(b.size() == 20 * N);
    for (std::size_t i = 0; i < N; ++i) {
        CHECK(std::isnan(cmd.position[i]) && cmd.velocity[i] == 0.0f && cmd.effort[i] == 0.0f);
        CHECK(std::isnan(cmd.kp[i]) && cmd.kd[i] == 2.0f);
    }
    m::JointState<N> s;
    CHECK(medidas(s) == 0);
    s.position[N - 1] = 0.5f;
    CHECK(medidas(s) == 1);
}

/// El .agmen normal (tests/joints_usuario.agmen) y la plantilla dan los mismos bytes, en los dos sentidos.
void check_same_as_generated() {
    m::JointState<12> a;
    pruebas_joints::JointState g;
    for (std::size_t i = 0; i < 12; ++i) {
        a.position[i] = g.position[i] = 0.25f * static_cast<float>(i);
        a.velocity[i] = g.velocity[i] = -1.5f * static_cast<float>(i);
        a.effort[i] = g.effort[i] = (i % 3 == 0) ? m::joints_detail::kNaN : 3.0f + static_cast<float>(i);
    }
    CHECK(bytes_of(a) == bytes_of(g));
    CHECK(w::encode(a, 3, 42) == w::encode(g, 3, 42));   // el paquete entero, sobre incluido
    CHECK(bytes_of(m::JointState<12>::decode_payload(bytes_of(g))) == bytes_of(g));
    CHECK(bytes_of(pruebas_joints::JointState::decode_payload(bytes_of(a))) == bytes_of(a));
    // Un paquete de la plantilla se lee con el tipo generado (y al revés): para la red es el mismo tipo.
    CHECK(bytes_of(w::decode<pruebas_joints::JointState>(w::encode(a, 1, 1)).message) == bytes_of(a));
    CHECK(bytes_of(w::decode<m::JointState<12>>(w::encode(g, 1, 1)).message) == bytes_of(g));
    // El comando con N = 2 y los mismos valores por defecto escritos en el .agmen (= nan, = 0).
    CHECK(bytes_of(m::JointCommand<2>{}) == bytes_of(pruebas_joints::JointCommand{}));
}

/// Errores claros: un payload de otro tamaño y un paquete de otro N.
void check_errors() {
    const w::Bytes corto(24, 0);
    try {
        (void)m::JointState<12>::decode_payload(corto);
        CHECK(false);
    } catch (const w::WireError& e) {
        CHECK(e.kind() == w::WireError::Kind::BadPayloadSize);
        CHECK(std::string(e.what()).find("144") != std::string::npos);
    }
    try {
        (void)w::decode<m::JointState<12>>(w::encode(m::JointState<2>{}, 0, 0));
        CHECK(false);
    } catch (const w::WireError& e) {
        CHECK(e.kind() == w::WireError::Kind::TypeMismatch);
        CHECK(std::string(e.what()).find("f32[12]") != std::string::npos);
    }
}

}  // namespace

int main() {
    const auto vectors = load_vectors();
    CHECK(vectors.size() == 4);
    check_vectors<m::JointState<2>, 2>(vectors);
    check_vectors<m::JointState<12>, 12>(vectors);
    check_vectors<m::JointCommand<2>, 2>(vectors);
    check_vectors<m::JointCommand<12>, 12>(vectors);
    check_order<2>(vectors);
    check_order<12>(vectors);
    check_defaults<1>();
    check_defaults<2>();
    check_defaults<12>();
    check_general<2>();
    check_general<12>();
    check_same_as_generated();
    check_errors();
    CHECK(joints_solo_check() == 0);
    std::puts(g_failures == 0 ? "joints: ok" : "joints: FALLA");
    return g_failures == 0 ? 0 : 1;
}
