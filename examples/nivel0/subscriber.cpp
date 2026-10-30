// Nivel 0 — subscriber.
//
// Recibe ImuSample por UDP y cada segundo imprime: mensajes recibidos,
// perdidos, desordenados, errores y latencia (min/prom/p99/max).
//
//   ./build/bin/subscriber --listen 127.0.0.1:7400

#include <agmen/agmen.hpp>

#include <chrono>
#include <cstdio>
#include <exception>
#include <optional>
#include <string>
#include <vector>

#include "common.hpp"

namespace {

void report(nivel0::Window& win,
            std::optional<std::chrono::steady_clock::time_point> last_msg_at) {
    using namespace std::chrono;
    if (const auto lat = win.latency_us()) {
        std::printf(
            "[subscriber] rx=%5llu perdidos=%-4llu desordenados=%-3llu errores=%-3llu "
            "latencia µs: min=%.0f prom=%.0f p99=%.0f max=%.0f\n",
            static_cast<unsigned long long>(win.received),
            static_cast<unsigned long long>(win.lost),
            static_cast<unsigned long long>(win.out_of_order),
            static_cast<unsigned long long>(win.errors), lat->min, lat->avg, lat->p99, lat->max);
    } else if (last_msg_at) {
        std::printf("[subscriber] sin mensajes desde hace %.1fs (errores=%llu)\n",
                    duration<double>(steady_clock::now() - *last_msg_at).count(),
                    static_cast<unsigned long long>(win.errors));
    } else {
        std::printf("[subscriber] esperando publisher... (errores=%llu)\n",
                    static_cast<unsigned long long>(win.errors));
    }
    std::fflush(stdout);
}

}  // namespace

int main(int argc, char** argv) try {
    using namespace std::chrono;
    namespace wire = agmen::wire;
    agmen::platform::init_console();

    if (nivel0::wants_help(argc, argv)) {
        std::puts(
            "subscriber [--listen IP:PUERTO] [--verbose 1]\n\n"
            "  --listen   dirección donde escuchar (default 127.0.0.1:7400)\n"
            "  --verbose  1 = imprime cada mensaje (default 0)");
        return 0;
    }
    const auto listen = nivel0::arg<std::string>(argc, argv, "listen", "127.0.0.1:7400");
    const auto verbose = nivel0::arg<int>(argc, argv, "verbose", 0);

    auto socket = agmen::net::UdpSocket::bind(listen);
    std::printf("[subscriber] Agmen %s · escuchando %s (type_id 0x%016llx) en %s\n",
                std::string(agmen::kVersion).c_str(), std::string(wire::ImuSample::kName).c_str(),
                static_cast<unsigned long long>(wire::type_id_v<wire::ImuSample>),
                listen.c_str());
    std::fflush(stdout);

    // 64 KiB = tamaño máximo de un datagrama UDP.
    std::vector<std::uint8_t> buf(65536);
    nivel0::SeqTracker seq;
    nivel0::Window win;
    auto window_start = steady_clock::now();
    std::optional<steady_clock::time_point> last_msg_at;
    std::string last_error;

    for (;;) {
        // Timeout corto para poder imprimir estadísticas aunque no llegue nada.
        if (const auto rx = socket.recv_from(buf, milliseconds(200))) {
            const auto recv_ns = wire::now_ns();
            try {
                const auto [h, msg] = wire::decode<wire::ImuSample>(
                    std::span<const std::uint8_t>(buf.data(), rx->size));
                ++win.received;
                last_msg_at = steady_clock::now();
                win.latencies_ns.push_back(recv_ns > h.stamp_ns ? recv_ns - h.stamp_ns : 0);
                switch (const auto [ev, lost] = seq.update(h.seq); ev) {
                    case nivel0::SeqEvent::First:
                        std::printf("[subscriber] primer mensaje de %s\n", rx->from.c_str());
                        break;
                    case nivel0::SeqEvent::Restart:
                        std::puts("[subscriber] el publisher se reinició");
                        break;
                    case nivel0::SeqEvent::Gap: win.lost += lost; break;
                    case nivel0::SeqEvent::OutOfOrder: ++win.out_of_order; break;
                    case nivel0::SeqEvent::InOrder: break;
                }
                if (verbose != 0) {
                    std::printf("  seq=%llu accel=[%g, %g, %g] gyro=[%g, %g, %g]\n",
                                static_cast<unsigned long long>(h.seq), msg.accel[0],
                                msg.accel[1], msg.accel[2], msg.gyro[0], msg.gyro[1],
                                msg.gyro[2]);
                }
            } catch (const wire::WireError& e) {
                ++win.errors;
                // Cada error distinto se muestra una vez, con su origen.
                std::string text = std::string(e.what()) + " (desde " + rx->from + ")";
                if (text != last_error) {
                    std::fprintf(stderr, "[subscriber] ERROR: %s\n", text.c_str());
                    std::fflush(stderr);
                    last_error = std::move(text);
                }
            }
        }

        if (steady_clock::now() - window_start >= seconds(1)) {
            report(win, last_msg_at);
            win = nivel0::Window{};
            window_start = steady_clock::now();
        }
    }
} catch (const std::exception& e) {
    std::fprintf(stderr, "[subscriber] error: %s\n", e.what());
    return 1;
}
