// <agmen/joints.hpp> se basta solo: esta unidad de traducción no incluye nada más de Agmen
// (tests/test_joints.cpp llama a joints_solo_check). Un JointState<29> y un JointCommand<29>
// con ningún otro include que este.
#include <agmen/joints.hpp>

#include <cmath>

int joints_solo_check() {
    static_assert(agmen::wire::Message<agmen::msgs::JointState<29>>);
    static_assert(agmen::msgs::JointState<29>::kSchema == "JointState{f32[29] position;f32[29] velocity;f32[29] effort}");
    const agmen::msgs::JointState<29> s;
    const agmen::msgs::JointCommand<29> c;
    agmen::wire::Bytes a, b;
    s.encode_payload(a);
    c.encode_payload(b);
    const auto back = agmen::msgs::JointCommand<29>::decode_payload(b);
    int bad = 0;
    if (a.size() != 12 * 29 || b.size() != 20 * 29) ++bad;
    if (!std::isnan(back.kp[28]) || back.velocity[28] != 0.0f) ++bad;
    const auto packet = agmen::wire::encode(s, 1, 2);
    if (packet.size() != agmen::wire::kEnvelopeLen + 12 * 29) ++bad;
    return bad;
}
