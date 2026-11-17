// El orden contrario de includes: <agmen/geometry.hpp> primero (debe incluir msgs.hpp por su cuenta).
#include <agmen/geometry.hpp>

int solo2_check() {
    constexpr auto q = agmen::msgs::from_wxyz(std::array<double, 4>{1, 0, 0, 0});
    static_assert(q.w == 1.0 && q.x == 0.0);
    return agmen::msgs::wxyz(q)[0] == 1.0 ? 0 : 1;
}
