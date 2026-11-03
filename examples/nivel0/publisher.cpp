// Nivel 0 — publisher.
//
// Manda ImuSample simulados por UDP a frecuencia fija, en el topic "/imu".
//
//   ./build/bin/publisher --rate 100 --to 127.0.0.1:7400

#include <agmen/agmen.hpp>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <exception>
#include <string>

#include "common.hpp"

int main(int argc, char** argv) try {
    using namespace std::chrono;
    namespace wire = agmen::wire;
    agmen::platform::init_console();

    if (nivel0::wants_help(argc, argv)) {
        std::puts(
            "publisher [--to IP:PUERTO] [--rate HZ] [--count N]\n\n"
            "  --to     destino           (default 127.0.0.1:7400)\n"
            "  --rate   frecuencia en Hz  (default 100)\n"
            "  --count  mensajes a enviar, 0 = infinito (default 0)");
        return 0;
    }
    const auto to = nivel0::arg<std::string>(argc, argv, "to", "127.0.0.1:7400");
    const auto rate = nivel0::arg<double>(argc, argv, "rate", 100.0);
    const auto count = nivel0::arg<std::uint64_t>(argc, argv, "count", 0);

    // El Publisher lleva seq, marcas de tiempo y anuncia el esquema.
    agmen::Publisher<wire::ImuSample> pub(to, "/imu");

    const auto period = duration_cast<steady_clock::duration>(duration<double>(1.0 / rate));
    std::printf("[publisher] Agmen %s · %s (schema_id 0x%016llx) → %s a %g Hz\n",
                std::string(agmen::kVersion).c_str(), wire::schema_text<wire::ImuSample>().c_str(),
                static_cast<unsigned long long>(wire::schema_id_v<wire::ImuSample>), to.c_str(),
                rate);
    std::fflush(stdout);

    const auto start = steady_clock::now();
    auto last_report = start;
    std::uint64_t seq = 0;
    std::uint64_t overruns = 0;   // ciclos en los que despertamos tarde (aunque sea 1 µs)
    nanoseconds worst_lag{0};     // el peor atraso contra el horario: si crece, nos estamos quedando atrás

    while (count == 0 || seq < count) {
        // IMU simulada: el robot oscila suavemente.
        const float t = duration<float>(steady_clock::now() - start).count();
        const wire::ImuSample msg{
            .accel = {0.5f * std::sin(2.0f * t), 0.5f * std::cos(2.0f * t), 9.81f},
            .gyro = {0.0f, 0.0f, 0.3f * std::sin(0.5f * t)},
        };

        // t_data = ahora (el instante de la "lectura"). Si nadie escucha,
        // publish() devuelve NoListener y seguimos publicando.
        (void)pub.publish(msg);
        ++seq;

        const auto now = steady_clock::now();
        if (now - last_report >= seconds(1)) {
            std::printf("[publisher] enviados=%llu retrasos=%llu\n",
                        static_cast<unsigned long long>(seq),
                        static_cast<unsigned long long>(overruns));
            std::fflush(stdout);
            last_report = now;
        }

        // Programación por "deadline": el mensaje k sale en start + k·periodo.
        // Si hiciéramos sleep(periodo) después de cada envío, el error se
        // acumularía y la frecuencia real sería menor que la pedida.
        const auto deadline = start + period * static_cast<std::int64_t>(seq);
        if (deadline > steady_clock::now()) {
            agmen::platform::sleep_until(deadline);
        } else {
            ++overruns;  // vamos tarde: no dormimos
            worst_lag = std::max(worst_lag, duration_cast<nanoseconds>(steady_clock::now() - deadline));
        }
    }

    std::printf("[publisher] terminado: %llu mensajes, %llu retrasos, atraso_max=%.1f ms\n",
                static_cast<unsigned long long>(seq), static_cast<unsigned long long>(overruns),
                duration<double, std::milli>(worst_lag).count());
    return 0;
} catch (const std::exception& e) {
    std::fprintf(stderr, "[publisher] error: %s\n", e.what());
    return 1;
}
