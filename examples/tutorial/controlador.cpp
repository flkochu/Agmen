// Tutorial, capítulo 2 — nodo "controlador".
//
// Manda MotorCommand a 50 Hz: el robot avanza haciendo eses suaves.
//
//   ./build/bin/tutorial_controlador        # para siempre
//   ./build/bin/tutorial_controlador 100    # manda 100 comandos y termina

#include <agmen/agmen.hpp>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>

#include "motor_command.hpp"

int main(int argc, char** argv) {
    using namespace std::chrono;
    namespace wire = agmen::wire;
    agmen::platform::init_console();

    const long count = argc > 1 ? std::atol(argv[1]) : 0;  // 0 = sin límite
    constexpr auto kPeriod = milliseconds(20);                // 50 Hz

    auto socket = agmen::net::UdpSocket::bind("0.0.0.0:0");
    socket.connect("127.0.0.1:7500");
    std::puts("[controlador] mandando MotorCommand a 127.0.0.1:7500 (50 Hz)");

    const auto start = steady_clock::now();
    for (long seq = 0; count == 0 || seq < count; ++seq) {
        const float t = duration<float>(steady_clock::now() - start).count();
        const MotorCommand cmd{
            .left = 0.5f + 0.3f * std::sin(t),
            .right = 0.5f - 0.3f * std::sin(t),
        };
        (void)socket.send(wire::encode(cmd, static_cast<std::uint64_t>(seq), wire::now_ns()));

        if (seq % 50 == 0) {
            std::printf("[controlador] t=%4.1fs  izq=%+.2f der=%+.2f\n", t, cmd.left, cmd.right);
            std::fflush(stdout);
        }
        agmen::platform::sleep_until(start + kPeriod * (seq + 1));
    }
    std::puts("[controlador] listo");
    return 0;
}
