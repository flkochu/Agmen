// El compañero de C++ de scripts/estandar_smoke.py: publica o recibe Odometry e Imu por un topic con el hub.
//   estandar_peer pub odometry|imu /topic N    publica N mensajes a 100 Hz; t_data = 5e9 + 1000*i
//   estandar_peer sub odometry|imu /topic N    recibe N mensajes y por cada uno imprime "RX t_data payload_hex"
// Los valores de cada mensaje i salen de i (los mismos que arma el script en Python).
#include <agmen/agmen.hpp>
#include <agmen/geometry.hpp>
#include <agmen/msgs.hpp>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <string>
#include <thread>

namespace {
namespace m = agmen::msgs;

std::string hex(const agmen::wire::Bytes& b) {
    static const char* d = "0123456789abcdef";
    std::string s;
    for (auto x : b) {
        s += d[x >> 4];
        s += d[x & 15];
    }
    return s;
}

m::Odometry odometry(int i) {
    m::Odometry o;
    o.pose.position = {i + 0.5, -static_cast<double>(i), 0.25};
    o.pose.orientation = m::from_wxyz(std::array<double, 4>{0.5, 0.5, -0.5, 0.5});
    o.linear_velocity = {0.1 * i, 0.2, 0.3};
    o.angular_velocity = {0.0, 0.0, -0.25};
    return o;
}

m::Imu imu(int i) {
    m::Imu u;   // la orientación se queda en NaN en los pares; en los impares se llena
    if (i % 2 == 1) u.orientation = m::from_wxyz(std::array<float, 4>{0.5f, 0.5f, -0.5f, 0.5f});
    u.angular_velocity = {0.5f * static_cast<float>(i), -0.02f, 0.03f};
    u.linear_acceleration = {0.0f, 0.0f, 9.81f};
    return u;
}

template <class M, class F>
int pub(const std::string& topic, int n, F make) {
    agmen::Publisher<M> p(topic);
    for (int i = 0; i < n; ++i) {
        (void)p.publish(make(i), 5'000'000'000LL + 1000LL * i);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return 0;
}

template <class M>
int sub(const std::string& topic, int n) {
    agmen::Subscriber<M> s(topic);
    std::printf("LISTO\n");
    std::fflush(stdout);
    int got = 0;
    const auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(20);
    while (got < n && std::chrono::steady_clock::now() < limit) {
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
}  // namespace

int main(int argc, char** argv) try {
    if (argc != 5) {
        std::fprintf(stderr, "uso: estandar_peer pub|sub odometry|imu /topic N\n");
        return 64;
    }
    const std::string mode = argv[1], type = argv[2], topic = argv[3];
    const int n = std::atoi(argv[4]);
    if (mode == "pub") return type == "odometry" ? pub<m::Odometry>(topic, n, odometry) : pub<m::Imu>(topic, n, imu);
    return type == "odometry" ? sub<m::Odometry>(topic, n) : sub<m::Imu>(topic, n);
} catch (const std::exception& e) {
    std::fprintf(stderr, "estandar_peer: error: %s\n", e.what());
    return 1;
}
