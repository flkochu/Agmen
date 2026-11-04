// El C++ que genera `agmen gen` (desde tests/vectors/v1.agmen) contra los
// vectores de prueba compartidos (tests/vectors/v1.txt):
//   - el texto canónico y el schema_id son los del vector;
//   - decodificar el payload del vector da los valores esperados;
//   - volver a codificar da EXACTAMENTE los mismos bytes (relleno incluido).
#include "v1.hpp"   // generado por agmen_add_messages(vectores_msgs tests/vectors/v1.agmen)

#include <agmen/msgs.hpp>

#include <cstdio>
#include <fstream>
#include <limits>
#include <map>
#include <sstream>
#include <string>

#ifndef AGMEN_VECTORS_FILE
#error "CMake debe definir AGMEN_VECTORS_FILE"
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

struct Vector {
    std::string text, id, payload_hex;
    bool arrow = false;
};

std::map<std::string, Vector> load_vectors() {
    std::map<std::string, Vector> out;
    std::ifstream in(AGMEN_VECTORS_FILE);
    std::string line;
    while (std::getline(in, line)) {
        const bool arrow = line.rfind("arrow\t", 0) == 0;
        if (!arrow && line.rfind("schema\t", 0) != 0) continue;
        std::vector<std::string> cols;
        std::stringstream ss(line);
        std::string col;
        while (std::getline(ss, col, '\t')) cols.push_back(col);
        // schema  entrada  canónico  schema_id  tamaño  valores  payload_hex
        // arrow   entrada  canónico  schema_id  valores  payload_hex
        const std::string& canon = cols.at(2);
        const std::string name = canon.substr(8, canon.find('{') - 8);   // "agmen/1 " + Nombre
        out[name] = Vector{canon, cols.at(3), cols.at(arrow ? 5 : 6), arrow};
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

/// Revisa texto, id y la ida y vuelta de bytes; devuelve el mensaje decodificado.
template <class M>
M check_type(const std::map<std::string, Vector>& vectors) {
    const std::string name(M::kName);
    const auto it = vectors.find(name);
    if (it == vectors.end()) {
        std::fprintf(stderr, "  FALLA: no hay vector para %s\n", name.c_str());
        ++g_failures;
        return M{};
    }
    const Vector& v = it->second;
    CHECK(agmen::wire::schema_text<M>() == v.text);
    CHECK(hex(agmen::wire::schema_id_v<M>) == v.id);
    const auto payload = from_hex(v.payload_hex);
    CHECK(v.arrow == agmen::wire::is_arrow_v<M>);
    if (!v.arrow) CHECK(payload.size() == M::kPayloadLen);
    const M m = M::decode_payload(payload);
    agmen::wire::Bytes again;
    m.encode_payload(again);
    if (again != payload) {
        std::fprintf(stderr, "  FALLA %s: volver a codificar no da los mismos bytes\n", name.c_str());
        ++g_failures;
    }
    std::printf("  ok %-14s %3zu bytes  %s%s\n", name.c_str(), payload.size(), v.id.c_str(), v.arrow ? "  (arrow)" : "");
    return m;
}

}  // namespace

int main() {
    using namespace vectores;
    const auto vectors = load_vectors();
    std::printf("C++ generado por agmen gen contra %zu vectores:\n", vectors.size());

    const auto mc = check_type<MotorCommand>(vectors);
    CHECK(mc.left == 0.5f && mc.right == -0.25f);

    const auto imu = check_type<ImuSample>(vectors);
    CHECK(imu.ay == -9.81f && imu.gz == -0.03f);

    const auto j = check_type<JointCommand7>(vectors);
    CHECK(j.mode == 3 && j.position[0] == 1.0 && j.position[6] == 7.0);

    const auto pose = check_type<Pose>(vectors);
    CHECK(pose.p.x == 1.5 && pose.p.y == -2.0 && pose.p.z == 0.25 && pose.yaw == 3.0f);

    const auto fl = check_type<Flags>(vectors);
    CHECK(fl.a && fl.b == 513 && fl.c == 7);

    const auto mat = check_type<Matriz>(vectors);
    CHECK(mat.m[0][0] == 1.0f && mat.m[0][2] == 3.0f && mat.m[1][0] == 4.0f && mat.m[1][2] == 6.0f);

    const auto ti = check_type<Tiempos>(vectors);
    CHECK(ti.t == 1700000000123456789LL && ti.d == -5 && ti.x == -1);

    const auto en = check_type<Enteros>(vectors);
    CHECK(en.a == std::numeric_limits<std::int8_t>::min() && en.b == std::numeric_limits<std::int16_t>::min());
    CHECK(en.c == std::numeric_limits<std::int32_t>::min() && en.d == -1);
    CHECK(en.e == 255 && en.f == 65535 && en.g == 4294967295U && en.h == std::numeric_limits<std::uint64_t>::max());

    // --- arrow (RFC 0008): decodificar y volver a codificar da los mismos bytes ---
    const auto tx = check_type<Texto>(vectors);
    CHECK(tx.s == "hola, ñandú" && tx.n == 7);

    const auto bl = check_type<Blob>(vectors);
    CHECK(bl.b.size() == 10 && bl.b[9] == 9 && bl.k == 513);

    const auto gr = check_type<Grande>(vectors);
    CHECK(gr.b.size() == 100 && gr.b[99] == 99);

    const auto li = check_type<Lista>(vectors);
    CHECK(li.x.size() == 3 && li.x[1] == -2.0f && li.y.empty() && li.z.size() == 4 && li.z[0] && !li.z[1]);

    const auto te = check_type<Tensor>(vectors);
    CHECK(te.m.shape == (std::vector<std::int32_t>{2, 3}) && te.m.data.size() == 6 && te.m.data[5] == 6.0f);
    CHECK(te.v.shape == (std::vector<std::int32_t>{0}) && te.v.data.empty());

    const auto im = check_type<Imagen>(vectors);
    CHECK(im.frame_id == "camara" && im.width == 2 && im.encoding == "rgb8" && im.data.size() == 12);
    CHECK(agmen::wire::schema_id_v<Imagen> == agmen::wire::schema_id_v<agmen::msgs::Imagen>);

    const auto mx = check_type<Mixto>(vectors);
    CHECK(mx.pts.size() == 2 && mx.pts[1].x == -1.0 && mx.tags.size() == 3 && mx.tags[2] == "ñ");
    CHECK(mx.m[1][1] == 4.0f && mx.c[2] == 9 && mx.pares[1].a == 2 && mx.pares[1].b == "dos" && mx.d == -5);

    // Un payload de otro tamaño: error claro, no basura.
    try {
        (void)MotorCommand::decode_payload(from_hex("0000003f"));
        std::fprintf(stderr, "  FALLA: un payload corto no dio error\n");
        ++g_failures;
    } catch (const agmen::wire::WireError& e) {
        CHECK(e.kind() == agmen::wire::WireError::Kind::BadPayloadSize);
    }

    if (g_failures) {
        std::fprintf(stderr, "%d fallas\n", g_failures);
        return 1;
    }
    std::puts("todo bien");
    return 0;
}
