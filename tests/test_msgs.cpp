// Los mensajes estándar chicos de <agmen/msgs.hpp> (RFC 0013 §3.3) contra los
// vectores de prueba compartidos (tests/vectors/estandar.txt), los mismos que
// revisan Python (python/test_msgs_estandar.py) y JavaScript (agmen.js):
//   - el texto canónico, el schema_id y el tamaño son los del vector;
//   - el valor por defecto de C++ (con sus NaN) da EXACTAMENTE los bytes del vector;
//   - decodificar cada payload y volver a codificar da los mismos bytes (NaN incluidos);
//   - wxyz() y from_wxyz() de <agmen/geometry.hpp> no cambian el orden por error;
//   - `= nan` en .agmen (tests/valores_nan.agmen: sueltos, arreglos y mezclados)
//     compila y da los mismos bits que Python (python/test_idl.py, TestNan).
// Con NaN, un mensaje no es igual a sí mismo (==): aquí se comparan bytes o se usa isnan.
#include "valores_nan.hpp"   // generado por agmen_add_messages(valores_nan_msgs tests/valores_nan.agmen)

#include <agmen/geometry.hpp>
#include <agmen/msgs.hpp>

#include <bit>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#ifndef AGMEN_ESTANDAR_FILE
#error "CMake debe definir AGMEN_ESTANDAR_FILE"
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

struct Tipo {
    std::string kind, text, id;
    std::size_t size = 0;
    std::map<std::string, std::string> valores;   // etiqueta → payload_hex
};

std::vector<std::string> columns(const std::string& line) {
    std::vector<std::string> cols;
    std::stringstream ss(line);
    std::string col;
    while (std::getline(ss, col, '\t')) cols.push_back(col);
    return cols;
}

std::map<std::string, Tipo> load_vectors() {
    std::map<std::string, Tipo> out;
    std::ifstream in(AGMEN_ESTANDAR_FILE);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();   // por si git lo cambió a CRLF
        const auto cols = columns(line);
        if (cols.empty()) continue;
        // tipo   nombre  struct|message  canónico  schema_id  tamaño
        // valor  nombre  etiqueta        valores   payload_hex
        if (cols[0] == "tipo") {
            Tipo& t = out[cols.at(1)];
            t.kind = cols.at(2);
            t.text = cols.at(3);
            t.id = cols.at(4);
            t.size = static_cast<std::size_t>(std::stoul(cols.at(5)));
        } else if (cols[0] == "valor") {
            out[cols.at(1)].valores[cols.at(2)] = cols.at(4);
        }
    }
    return out;
}

agmen::wire::Bytes from_hex(const std::string& h) {
    agmen::wire::Bytes b;
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

template <class T>
agmen::wire::Bytes bytes_of(const T& v) {
    agmen::wire::Bytes out;
    v.encode_into(out);
    return out;
}

/// Revisa un tipo contra su vector y devuelve sus valores decodificados (etiqueta → valor).
template <class T>
std::map<std::string, T> check_type(const std::map<std::string, Tipo>& vectors, const char* kind) {
    std::map<std::string, T> decoded;
    const std::string name(T::kName);
    const auto it = vectors.find(name);
    if (it == vectors.end()) {
        std::fprintf(stderr, "  FALLA: no hay vector para %s\n", name.c_str());
        ++g_failures;
        return decoded;
    }
    const Tipo& v = it->second;
    const std::string text = std::string(agmen::wire::kSchemaPrefix) + std::string(T::kSchema);
    const std::uint64_t id = agmen::wire::fnv1a64(T::kSchema, agmen::wire::fnv1a64(agmen::wire::kSchemaPrefix));
    CHECK(text == v.text);
    CHECK(hex(id) == v.id);
    CHECK(T::kPayloadLen == v.size);
    CHECK(v.kind == kind);
    CHECK(v.valores.count("por_defecto") == 1 && v.valores.size() >= 2);
    for (const auto& [etiqueta, payload_hex] : v.valores) {
        const auto payload = from_hex(payload_hex);
        CHECK(payload.size() == T::kPayloadLen);
        const T m = T::decode_at(payload, 0);
        if (bytes_of(m) != payload) {
            std::fprintf(stderr, "  FALLA %s %s: volver a codificar no da los mismos bytes\n", name.c_str(),
                         etiqueta.c_str());
            ++g_failures;
        }
        decoded[etiqueta] = m;
    }
    // El valor por defecto de C++ (T{}) es el de Python, bit por bit (NaN incluidos).
    if (bytes_of(T{}) != from_hex(v.valores.at("por_defecto"))) {
        std::fprintf(stderr, "  FALLA %s: el valor por defecto de C++ no da los bytes del vector\n", name.c_str());
        ++g_failures;
    }
    std::printf("  ok %-12s %3zu bytes  %s  (%zu valores)\n", name.c_str(), T::kPayloadLen, v.id.c_str(),
                v.valores.size());
    return decoded;
}

/// Lo mismo, y además lo que solo tiene un message: encode_payload, decode_payload y los rasgos del sobre.
template <class M>
std::map<std::string, M> check_message(const std::map<std::string, Tipo>& vectors) {
    static_assert(agmen::wire::Message<M>);
    static_assert(!agmen::wire::is_arrow_v<M>, "los mensajes chicos van en compact");
    static_assert(agmen::wire::compact_size_v<M> == M::kPayloadLen);
    auto decoded = check_type<M>(vectors, "message");
    CHECK(agmen::wire::schema_text<M>() == vectors.at(std::string(M::kName)).text);
    for (const auto& [etiqueta, m] : decoded) {
        agmen::wire::Bytes a;
        m.encode_payload(a);
        CHECK(a == bytes_of(m));
        CHECK(bytes_of(M::decode_payload(a)) == a);
    }
    return decoded;
}

bool all_nan(const agmen::msgs::Quaternionf& q) {
    return std::isnan(q.x) && std::isnan(q.y) && std::isnan(q.z) && std::isnan(q.w);
}

}  // namespace

// Los tamaños del RFC 0013 §3.3, revisados por el compilador.
static_assert(agmen::msgs::Vector2::kPayloadLen == 16);
static_assert(agmen::msgs::Vector3::kPayloadLen == 24);
static_assert(agmen::msgs::Quaternion::kPayloadLen == 32);
static_assert(agmen::msgs::Pose::kPayloadLen == 56);
static_assert(agmen::msgs::Transform::kPayloadLen == 56);
static_assert(agmen::msgs::Twist::kPayloadLen == 48);
static_assert(agmen::msgs::Odometry::kPayloadLen == 104);
static_assert(agmen::msgs::Vector3f::kPayloadLen == 12);
static_assert(agmen::msgs::Quaternionf::kPayloadLen == 16);
static_assert(agmen::msgs::Imu::kPayloadLen == 40);
// nan en .agmen: un NaN quieto con los mismos bits en GCC, clang y MSVC.
static_assert(std::bit_cast<std::uint32_t>(agmen::msgs::Quaternionf{}.x) == 0x7FC00000U);
static_assert(std::bit_cast<std::uint32_t>(agmen::msgs::Quaternionf{}.w) == 0x7FC00000U);
static_assert(agmen::msgs::Quaternion{}.w == 1.0);

int main() {
    using namespace agmen::msgs;
    const auto vectors = load_vectors();
    std::printf("Mensajes estándar de <agmen/msgs.hpp> contra %zu tipos de %s:\n", vectors.size(), AGMEN_ESTANDAR_FILE);
    CHECK(vectors.size() == 10);

    check_type<Vector2>(vectors, "struct");
    const auto v3 = check_type<Vector3>(vectors, "struct");
    const auto q = check_type<Quaternion>(vectors, "struct");
    const auto pose = check_message<Pose>(vectors);
    check_message<Transform>(vectors);
    const auto tw = check_message<Twist>(vectors);
    const auto odo = check_message<Odometry>(vectors);
    const auto v3f = check_type<Vector3f>(vectors, "struct");
    const auto qf = check_type<Quaternionf>(vectors, "struct");
    const auto imu = check_message<Imu>(vectors);

    // Cada número en su campo (un campo cambiado de lugar daría los mismos bytes de ida y vuelta).
    CHECK(v3.at("ejemplo").x == 1.5 && v3.at("ejemplo").y == -2.25 && v3.at("ejemplo").z == 0.125);
    const Quaternion& r = q.at("rotacion");
    CHECK(r.x == 0.0916432938695913 && r.y == 0.1832865877391826 && r.z == 0.2749298816087739 &&
          r.w == 0.9393727128473789);
    CHECK(q.at("por_defecto").w == 1.0 && q.at("por_defecto").x == 0.0);
    CHECK(pose.at("ejemplo").position.y == -2.25 && pose.at("ejemplo").orientation.w == r.w);
    CHECK(tw.at("ejemplo").linear.x == 0.5 && tw.at("ejemplo").angular.z == 0.3);
    CHECK(odo.at("ejemplo").pose.position.x == 12.5 && odo.at("ejemplo").angular_velocity.z == -0.25);
    CHECK(v3f.at("ejemplo").y == -9.81f);
    CHECK(all_nan(qf.at("por_defecto")) && qf.at("rotacion").w == static_cast<float>(r.w));
    CHECK(all_nan(imu.at("por_defecto").orientation) && imu.at("por_defecto").linear_acceleration.z == 0.0f);
    CHECK(all_nan(imu.at("sin_orientacion").orientation) && imu.at("sin_orientacion").linear_acceleration.z == 9.81f);
    CHECK(imu.at("ejemplo").linear_acceleration.z == 9.81f && imu.at("ejemplo").angular_velocity.y == -0.02f);

    // Los bits del NaN por defecto, también al codificar (0x7FC00000 en little endian).
    {
        const auto b = bytes_of(Quaternionf{});
        CHECK(b.size() == 16 && b[0] == 0x00 && b[1] == 0x00 && b[2] == 0xC0 && b[3] == 0x7F);
    }

    // wxyz() / from_wxyz(): w primero (MuJoCo), con una rotación que no es simétrica.
    {
        const std::array<double, 4> a = wxyz(r);
        CHECK(a[0] == r.w && a[1] == r.x && a[2] == r.y && a[3] == r.z);
        const Quaternion back = from_wxyz(a);
        CHECK(bytes_of(back) == bytes_of(r));
        const double mujoco[7] = {1.0, 2.0, 3.0, r.w, r.x, r.y, r.z};   // qpos de un cuerpo libre: posición y w, x, y, z
        const Quaternion from_ptr = from_wxyz(std::span<const double, 4>(mujoco + 3, 4));
        CHECK(bytes_of(from_ptr) == bytes_of(r));

        const Quaternionf& rf = qf.at("rotacion");
        const std::array<float, 4> af = wxyz(rf);
        CHECK(af[0] == rf.w && af[1] == rf.x && af[2] == rf.y && af[3] == rf.z);
        CHECK(bytes_of(from_wxyz(af)) == bytes_of(rf));
        CHECK(std::isnan(wxyz(Quaternionf{})[0]));   // solo cambia el orden: lo vacío sigue vacío
        static_assert(wxyz(Quaternion{})[0] == 1.0);
    }

    // nan en .agmen, en un archivo cualquiera: los mismos bytes que agmen.load en Python (TestNan en test_idl.py).
    {
        const pruebas_nan::ConNan c;
        CHECK(std::isnan(c.a) && std::isnan(c.b) && std::isnan(c.v[2]) && std::isnan(c.w[1]));
        CHECK(std::isnan(c.m[0]) && c.m[1] == 1.5f && std::isnan(c.m[2]) && c.c == 2.5 && c.k == 7);
        agmen::wire::Bytes b;
        c.encode_payload(b);
        CHECK(b == from_hex("0000c07f00000000000000000000f87f0000c07f0000c07f0000c07f00000000000000000000f87f00000000"
                            "0000f87f0000c07f0000c03f0000c07f0000000000000000000004400700000000000000"));
        std::printf("  ok %-12s %3zu bytes  (nan en .agmen: sueltos, arreglos y mezclados)\n", "ConNan", b.size());
    }

    if (g_failures) {
        std::fprintf(stderr, "%d fallas\n", g_failures);
        return 1;
    }
    std::puts("todo bien");
    return 0;
}
