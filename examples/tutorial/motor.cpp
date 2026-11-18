// Tutorial, capítulo 2 — nodo "motor".
//
// Recibe MotorCommand y "mueve" las ruedas (aquí solo imprime). Incluye la
// regla de seguridad más importante de un robot: si deja de recibir órdenes,
// se detiene solo.
//
//   ./build/bin/tutorial_motor                   # para siempre (Ctrl+C para salir)
//   ./build/bin/tutorial_motor 5                 # 5 segundos y muestra un resumen
//   ./build/bin/tutorial_motor --escuchar /cmd   # por topic (capítulo 9)

#include <agmen/agmen.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "motor_command.hpp"

int main(int argc, char** argv) {
    using namespace std::chrono;
    namespace wire = agmen::wire;
    agmen::platform::init_console();

    double duration_s = 0.0;                // 0 = sin límite
    std::string where = "127.0.0.1:7500";   // una dirección fija o un topic ("/cmd")
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--escuchar" && i + 1 < argc) {
            where = argv[++i];
        } else {
            duration_s = std::atof(argv[i]);
        }
    }
    constexpr auto kSafetyTimeout = milliseconds(500);

    agmen::Subscriber<MotorCommand> sub(where);
    std::printf("[motor] Agmen %s · esperando MotorCommand en %s\n",
                std::string(agmen::kVersion).c_str(), where.c_str());

    MotorCommand current{};  // empieza detenido
    bool stopped = true;
    long received = 0, errors = 0, non_finite = 0;
    bool non_finite_said = false;
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
                if (!std::isfinite(cmd.left) || !std::isfinite(cmd.right)) {
                    // NaN o infinito: std::clamp no lo arregla (con NaN devuelve NaN). No se usa: alto ya.
                    ++non_finite;
                    current = MotorCommand{};
                    stopped = true;
                    if (!non_finite_said) {
                        non_finite_said = true;
                        std::fprintf(stderr,
                                     "[motor] ⛔ ALTO: llegó un comando con un valor no finito (izq=%f der=%f) "
                                     "desde %s; no lo uso. Revisa ese nodo.\n",
                                     static_cast<double>(cmd.left), static_cast<double>(cmd.right),
                                     sub.last_from().c_str());
                    }
                } else {
                    non_finite_said = false;   // el aviso es por episodio: si vuelve un no finito, se dice otra vez
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

    std::printf("[motor] resumen: recibidos=%ld errores=%ld no_finitos=%ld\n", received, errors, non_finite);
    return errors == 0 ? 0 : 1;
}
