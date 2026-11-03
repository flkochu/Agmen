// Tutorial, capítulo 2 — nodo "motor".
//
// Recibe MotorCommand y "mueve" las ruedas (aquí solo imprime). Incluye la
// regla de seguridad más importante de un robot: si deja de recibir órdenes,
// se detiene solo.
//
//   ./build/bin/tutorial_motor          # corre para siempre (Ctrl+C para salir)
//   ./build/bin/tutorial_motor 5        # corre 5 segundos y muestra un resumen

#include <agmen/agmen.hpp>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "motor_command.hpp"

int main(int argc, char** argv) {
    using namespace std::chrono;
    namespace wire = agmen::wire;
    agmen::platform::init_console();

    const double duration_s = argc > 1 ? std::atof(argv[1]) : 0.0;  // 0 = sin límite
    constexpr auto kSafetyTimeout = milliseconds(500);

    agmen::Subscriber<MotorCommand> sub("127.0.0.1:7500");
    std::printf("[motor] Agmen %s · esperando MotorCommand en 127.0.0.1:7500\n",
                std::string(agmen::kVersion).c_str());

    MotorCommand current{};  // empieza detenido
    bool stopped = true;
    long received = 0, errors = 0;
    std::string last_error;
    const auto start = steady_clock::now();
    auto last_cmd = start;
    auto last_print = start;

    for (;;) {
        const auto now = steady_clock::now();
        if (duration_s > 0 && now - start >= duration<double>(duration_s)) break;

        try {
            if (auto sample = sub.receive(milliseconds(50))) {
                MotorCommand cmd = sample->message;
                // Nunca confíes ciegamente en un comando: recórtalo al rango válido.
                cmd.left = std::clamp(cmd.left, -1.0f, 1.0f);
                cmd.right = std::clamp(cmd.right, -1.0f, 1.0f);
                current = cmd;
                last_cmd = steady_clock::now();
                ++received;
                if (stopped) {
                    std::puts("[motor] ✅ recibiendo comandos, ruedas activas");
                    stopped = false;
                }
            }
        } catch (const wire::WireError& e) {
            ++errors;
            // Cada error distinto se muestra una vez (no llenar la terminal).
            std::string text = std::string(e.what()) + " (desde " + sub.last_from() + ")";
            if (text != last_error) {
                std::fprintf(stderr, "[motor] ERROR: %s\n", text.c_str());
                last_error = std::move(text);
            }
        }

        // Regla de seguridad: sin órdenes recientes → alto total.
        if (!stopped && steady_clock::now() - last_cmd > kSafetyTimeout) {
            current = MotorCommand{};
            stopped = true;
            std::puts("[motor] ⛔ ALTO: sin comandos desde hace 0.5 s (timeout de seguridad)");
        }

        if (steady_clock::now() - last_print >= milliseconds(500) && !stopped) {
            std::printf("[motor] rueda izq=%+.2f  der=%+.2f\n", current.left, current.right);
            last_print = steady_clock::now();
        }
        std::fflush(stdout);
    }

    std::printf("[motor] resumen: recibidos=%ld errores=%ld\n", received, errors);
    return errors == 0 ? 0 : 1;
}
