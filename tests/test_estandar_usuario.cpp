// Un .agmen de usuario que importa "agmen/msgs.agmen" (tests/usuario_msgs.agmen): el C++ generado
// compila y da los mismos bytes que Python. Imprime el payload de un Dron fijo (y el vacío) en hex;
// python/test_msgs_estandar_extra.py y scripts/estandar_smoke.py lo comparan con el suyo.
#include "usuario_msgs.hpp"

#include <agmen/geometry.hpp>

#include <bit>
#include <cmath>
#include <cstdio>
#include <string>

namespace {
std::string hex(const agmen::wire::Bytes& b) {
    static const char* d = "0123456789abcdef";
    std::string s;
    for (auto x : b) {
        s += d[x >> 4];
        s += d[x & 15];
    }
    return s;
}
}  // namespace

int main() {
    using namespace pruebas_usuario;
    Dron v{};
    // Los NaN por defecto son los de siempre: f32 0x7FC00000, f64 0x7FF8000000000000.
    int bad = 0;
    if (std::bit_cast<std::uint32_t>(v.calib[1]) != 0x7FC00000U) ++bad;
    if (std::bit_cast<std::uint64_t>(v.bateria) != 0x7FF8000000000000ULL) ++bad;
    if (v.giro.w != 1.0 || v.pose.orientation.w != 1.0) ++bad;
    agmen::wire::Bytes empty;
    v.encode_payload(empty);

    v.pose.position = {1.5, -2.25, 0.125};
    v.pose.orientation = agmen::msgs::from_wxyz(std::array<double, 4>{0.5, 0.5, -0.5, 0.5});
    v.viento = {0.1, 0.2, 0.3};
    v.giro = agmen::msgs::from_wxyz(std::array<double, 4>{1, 0, 0, 0});
    v.orden.linear = {0.5, 0, 0};
    v.orden.angular = {0, 0, -0.3};
    v.calib = {1.0f, std::bit_cast<float>(0x7FC00000U), 3.0f};
    v.id = 9;
    agmen::wire::Bytes full;
    v.encode_payload(full);
    std::printf("vacio %s\nlleno %s\nmalos %d\n", hex(empty).c_str(), hex(full).c_str(), bad);
    return bad == 0 ? 0 : 1;
}
