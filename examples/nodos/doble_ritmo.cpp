// Un nodo, dos ritmos (capítulo 12, RFC 0006).
//
//   - "control" a 1 kHz: manda MotorCommand cada milisegundo.
//   - "ia" a 10 Hz: un cálculo pesado (~80 ms de CPU) que decide el plan.
//
// Con la IA en su propio grupo (otro hilo), el control no pierde ciclos. Con
// --mismo-grupo, la IA corre en el grupo principal y el control se atrasa:
// así se ve por qué existen los grupos.
//
//   ./build/bin/doble_ritmo                 # para siempre (Ctrl+C)
//   ./build/bin/doble_ritmo 3               # 3 segundos y un resumen
//   ./build/bin/doble_ritmo 3 --mismo-grupo # lo mismo, sin grupo para la IA

#include <agmen/agmen.hpp>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "motor_command.hpp"

namespace {

// Un "planeador" de mentira: gasta CPU de verdad durante `ms` milisegundos.
float planear(int ms) {
    using clk = std::chrono::steady_clock;
    const auto end = clk::now() + std::chrono::milliseconds(ms);
    double x = 0;
    for (long i = 0; clk::now() < end; ++i) x += std::sin(static_cast<double>(i) * 1e-3);
    return static_cast<float>(0.5 + 0.1 * std::tanh(x));
}

}  // namespace

int main(int argc, char** argv) {
    using namespace std::chrono_literals;
    agmen::platform::init_console();
    double seconds = 0;
    bool same_group = false;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--mismo-grupo") == 0) {
            same_group = true;
        } else {
            seconds = std::atof(argv[i]);
        }
    }

    agmen::Node node("doble_ritmo");
    auto pub = node.publisher<MotorCommand>("/cmd");
    agmen::Latest<float> plan;   // la IA escribe, el control lee: seguro entre hilos

    node.every(1ms, [&] {        // 1 kHz, en el grupo principal
        const float v = plan.get_or(0.0f);
        (void)pub.publish(MotorCommand{.left = v, .right = v});
    });

    const agmen::Group ia = same_group ? agmen::Group{} : node.group("ia");
    node.every(100ms, [&] { plan.set(planear(80)); }, ia);   // 10 Hz, ~80 ms de cálculo

    std::printf("[doble_ritmo] control a 1 kHz + IA de 80 ms a 10 Hz en %s (reloj %s)\n",
                same_group ? "el MISMO grupo" : "OTRO grupo", std::string(agmen::clock::mode_name()).c_str());
    std::fflush(stdout);
    if (seconds > 0) {
        node.spin_for(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::duration<double>(seconds)));
    } else {
        node.spin();
    }

    const auto t = node.timers();
    for (const auto& s : t) {
        std::printf("[doble_ritmo] %s (%s): %llu veces, %llu saltados, peor atraso %.2f ms, lo más lento %.1f ms\n",
                    s.name.c_str(), s.group.empty() ? "principal" : s.group.c_str(),
                    static_cast<unsigned long long>(s.runs), static_cast<unsigned long long>(s.skipped),
                    static_cast<double>(s.worst_late_ns) / 1e6, static_cast<double>(s.worst_run_ns) / 1e6);
    }
    // Para scripts/paralelo_check.py
    std::printf("RESULTADO ciclos=%llu saltados=%llu peor_ms=%.3f\n", static_cast<unsigned long long>(t[0].runs),
                static_cast<unsigned long long>(t[0].skipped), static_cast<double>(t[0].worst_late_ns) / 1e6);
    return 0;
}
