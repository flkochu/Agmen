// Utilidades compartidas por los ejemplos del nivel 0.
#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

namespace nivel0 {

/// Lee `--nombre valor` de la línea de comandos. (Sin dependencias a
/// propósito; más adelante el CLI `agmen` tendrá un parser de verdad.)
template <class T>
T arg(int argc, char** argv, std::string_view name, T fallback) {
    const std::string flag = "--" + std::string(name);
    for (int i = 1; i < argc; ++i) {
        if (flag != argv[i]) continue;
        if (i + 1 >= argc) break;
        std::istringstream in(argv[i + 1]);
        T value{};
        if (in >> value && in.eof()) return value;
        break;
    }
    for (int i = 1; i < argc; ++i) {
        if (flag == argv[i]) {
            std::fprintf(stderr, "error: valor inválido o faltante para %s\n", flag.c_str());
            std::exit(2);
        }
    }
    return fallback;
}

inline bool wants_help(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        const std::string_view a = argv[i];
        if (a == "-h" || a == "--help") return true;
    }
    return false;
}

/// Qué pasó con el número de secuencia de un mensaje.
enum class SeqEvent { First, InOrder, Gap, OutOfOrder, Restart };

/// Seguimiento de números de secuencia para detectar pérdidas.
class SeqTracker {
public:
    /// Devuelve el evento y, si es Gap, cuántos mensajes se perdieron.
    std::pair<SeqEvent, std::uint64_t> update(std::uint64_t seq) {
        SeqEvent ev = SeqEvent::InOrder;
        std::uint64_t lost = 0;
        if (!expected_) {
            ev = SeqEvent::First;
        } else if (seq == 0) {
            ev = SeqEvent::Restart;
        } else if (seq == *expected_) {
            ev = SeqEvent::InOrder;
        } else if (seq > *expected_) {
            ev = SeqEvent::Gap;
            lost = seq - *expected_;
        } else {
            return {SeqEvent::OutOfOrder, 0};  // no movemos expected_
        }
        expected_ = seq + 1;
        return {ev, lost};
    }

private:
    std::optional<std::uint64_t> expected_;
};

/// Estadísticas de una ventana de recepción.
struct Window {
    std::uint64_t received = 0;
    std::uint64_t lost = 0;
    std::uint64_t out_of_order = 0;
    std::uint64_t errors = 0;
    std::vector<std::uint64_t> latencies_ns;

    struct LatencyUs {
        double min, avg, p99, max;
    };

    std::optional<LatencyUs> latency_us() {
        if (latencies_ns.empty()) return std::nullopt;
        std::sort(latencies_ns.begin(), latencies_ns.end());
        const auto n = latencies_ns.size();
        double sum = 0;
        for (auto v : latencies_ns) sum += static_cast<double>(v);
        const auto idx = std::min(static_cast<std::size_t>(static_cast<double>(n) * 0.99), n - 1);
        return LatencyUs{static_cast<double>(latencies_ns.front()) / 1e3,
                         sum / static_cast<double>(n) / 1e3,
                         static_cast<double>(latencies_ns[idx]) / 1e3,
                         static_cast<double>(latencies_ns.back()) / 1e3};
    }
};

}  // namespace nivel0
