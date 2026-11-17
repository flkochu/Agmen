// Los dos headers se bastan solos: este archivo incluye SOLO <agmen/msgs.hpp> y <agmen/geometry.hpp>
// (en ese orden y en el contrario, en dos unidades de traducción: ver test_geometry_solo2.cpp) y revisa
// wxyz()/from_wxyz() en constexpr, ida y vuelta bit por bit y el orden de los campos.
#include <agmen/msgs.hpp>
#include <agmen/geometry.hpp>

#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstdio>

int solo2_check();   // test_geometry_solo2.cpp: <agmen/geometry.hpp> solo, sin msgs.hpp antes

namespace {
using namespace agmen::msgs;

constexpr std::array<double, 4> kQ{0.1, 0.2, 0.3, 0.4};   // w, x, y, z
constexpr Quaternion kR = from_wxyz(kQ);
static_assert(kR.w == 0.1 && kR.x == 0.2 && kR.y == 0.3 && kR.z == 0.4);
static_assert(wxyz(kR) == kQ);
static_assert(wxyz(Quaternion{}) == std::array<double, 4>{1.0, 0.0, 0.0, 0.0});
constexpr std::array<float, 4> kQf{0.5f, 0.25f, -0.125f, 2.0f};
constexpr Quaternionf kRf = from_wxyz(kQf);
static_assert(kRf.w == 0.5f && kRf.x == 0.25f && kRf.y == -0.125f && kRf.z == 2.0f);
static_assert(wxyz(kRf) == kQf);
static_assert(noexcept(wxyz(kR)) && noexcept(from_wxyz(kQ)));
// std::span de tamaño fijo desde un puntero (como MuJoCo: d->qpos + 3)
constexpr double kBuf[7] = {9, 9, 9, 0.7, 0.1, 0.2, 0.3};
static_assert(from_wxyz(std::span<const double, 4>(kBuf + 3, 4)).w == 0.7);

int g = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FALLA %s:%d %s\n", __FILE__, __LINE__, #c); ++g; } } while (0)

bool same_bits(double a, double b) { return std::bit_cast<std::uint64_t>(a) == std::bit_cast<std::uint64_t>(b); }
}  // namespace

int main() {
    const double vals[] = {0.0, -0.0, 1.0, -1.0, 1e-300, 1e300, 0.1, -3.5e-7, 5e-324,
                           std::bit_cast<double>(0x7FF8000000000000ULL), std::bit_cast<double>(0x7FF8000000000123ULL),
                           std::bit_cast<double>(0xFFF8000000000000ULL), std::bit_cast<double>(0x7FF0000000000000ULL),
                           std::bit_cast<double>(0xFFF0000000000000ULL)};
    for (double a : vals) {
        for (double b : vals) {
            const std::array<double, 4> q{a, b, a, b};
            const auto r = wxyz(from_wxyz(q));
            for (int i = 0; i < 4; ++i) CHECK(same_bits(r[static_cast<std::size_t>(i)], q[static_cast<std::size_t>(i)]));
        }
    }
    // Un Quaternionf vacío sigue vacío (solo cambia el orden).
    const auto e = wxyz(Quaternionf{});
    for (float f : e) CHECK(std::bit_cast<std::uint32_t>(f) == 0x7FC00000U);
    // Nombre y orden de los campos: x, y, z, w en memoria.
    static_assert(offsetof(Quaternion, x) == 0 && offsetof(Quaternion, w) == 24);
    static_assert(sizeof(Quaternion) == 32 && sizeof(Quaternionf) == 16);
    CHECK(solo2_check() == 0);
    std::puts(g == 0 ? "geometry solo: ok" : "geometry solo: FALLA");
    return g == 0 ? 0 : 1;
}
