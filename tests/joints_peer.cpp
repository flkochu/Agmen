// El compañero de C++ de scripts/estandar_smoke.py para las articulaciones (RFC 0013 §3.4, D5 = f):
// JointState<N> y JointCommand<N> de <agmen/joints.hpp> por un topic con el hub.
//   joints_peer pub state|command N /topic K       publica K mensajes a 100 Hz; t_data = 5e9 + 1000*i
//   joints_peer sub state|command N /topic K       recibe K mensajes y por cada uno imprime "RX t_data payload_hex"
//   joints_peer rechazo state|command N /topic     publica hasta que el hub lo rechace (máx. 15 s) e imprime
//                                                  "RECHAZADO <motivo>"; si nunca lo rechaza, termina con 3
// N es 2 o 12. Los valores del mensaje i salen de i (los mismos que arma el script en Python), con NaN
// en varios lugares para ver que llegan intactos.
#include <agmen/agmen.hpp>
#include <agmen/joints.hpp>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <limits>
#include <string>
#include <thread>

namespace {
namespace m = agmen::msgs;

constexpr float kNaN = std::numeric_limits<float>::quiet_NaN();

std::string hex(const agmen::wire::Bytes& b) {
    static const char* d = "0123456789abcdef";
    std::string s;
    for (auto x : b) {
        s += d[x >> 4];
        s += d[x & 15];
    }
    return s;
}

// Estado i: position[j] = i + j/8; velocity[j] = -(i + j) en las impares, NaN en las pares; effort, NaN
// (no se mide).
template <std::size_t N>
m::JointState<N> state(int i) {
    m::JointState<N> s;
    for (std::size_t j = 0; j < N; ++j) {
        const float fj = static_cast<float>(j);
        s.position[j] = static_cast<float>(i) + 0.125f * fj;
        s.velocity[j] = j % 2 == 1 ? -(static_cast<float>(i) + fj) : kNaN;
    }
    return s;
}

// Comando i: position[j] = i + j/2; velocity 0 y effort = -j/4; kp = 20 + j en los i impares y NaN (las
// del grupo) en los pares; kd, NaN siempre.
template <std::size_t N>
m::JointCommand<N> command(int i) {
    m::JointCommand<N> c;
    for (std::size_t j = 0; j < N; ++j) {
        const float fj = static_cast<float>(j);
        c.position[j] = static_cast<float>(i) + 0.5f * fj;
        c.effort[j] = -0.25f * fj;
        if (i % 2 == 1) c.kp[j] = 20.0f + fj;
    }
    return c;
}

template <class M, class F>
int pub(const std::string& topic, int k, F make) {
    agmen::Publisher<M> p(topic);
    for (int i = 0; i < k; ++i) {
        (void)p.publish(make(i), 5'000'000'000LL + 1000LL * i);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return 0;
}

template <class M>
int sub(const std::string& topic, int k) {
    agmen::Subscriber<M> s(topic);
    std::printf("LISTO\n");
    std::fflush(stdout);
    int got = 0;
    const auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(20);
    while (got < k && std::chrono::steady_clock::now() < limit) {
        if (auto r = s.receive(std::chrono::milliseconds(100))) {
            agmen::wire::Bytes b;
            r->message.encode_payload(b);
            std::printf("RX %lld %s\n", static_cast<long long>(r->envelope.t_data), hex(b).c_str());
            std::fflush(stdout);
            ++got;
        }
    }
    return got >= 1 ? 0 : 2;
}

template <class M>
int rechazo(const std::string& topic) {
    const auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(15);
    try {
        agmen::Publisher<M> p(topic);
        while (std::chrono::steady_clock::now() < limit) {
            (void)p.publish(M{});
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    } catch (const agmen::wire::WireError& e) {
        std::printf("RECHAZADO %s\n", e.what());
        return 0;
    }
    std::printf("NO LO RECHAZÓ en 15 s\n");
    return 3;
}

template <std::size_t N>
int run(const std::string& mode, const std::string& kind, const std::string& topic, int k) {
    if (kind == "state") {
        if (mode == "pub") return pub<m::JointState<N>>(topic, k, state<N>);
        if (mode == "sub") return sub<m::JointState<N>>(topic, k);
        return rechazo<m::JointState<N>>(topic);
    }
    if (mode == "pub") return pub<m::JointCommand<N>>(topic, k, command<N>);
    if (mode == "sub") return sub<m::JointCommand<N>>(topic, k);
    return rechazo<m::JointCommand<N>>(topic);
}
}  // namespace

int main(int argc, char** argv) try {
    const std::string mode = argc > 1 ? argv[1] : "", kind = argc > 2 ? argv[2] : "", n = argc > 3 ? argv[3] : "";
    const bool ok = (argc == 6 && (mode == "pub" || mode == "sub")) || (argc == 5 && mode == "rechazo");
    if (!ok || (kind != "state" && kind != "command") || (n != "2" && n != "12")) {
        std::fprintf(stderr, "uso: joints_peer pub|sub state|command 2|12 /topic K\n"
                             "     joints_peer rechazo state|command 2|12 /topic\n");
        return 64;
    }
    const std::string topic = argv[4];
    const int k = argc == 6 ? std::atoi(argv[5]) : 0;
    return n == "2" ? run<2>(mode, kind, topic, k) : run<12>(mode, kind, topic, k);
} catch (const std::exception& e) {
    std::fprintf(stderr, "joints_peer: error: %s\n", e.what());
    return 1;
}
