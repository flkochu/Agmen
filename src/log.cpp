#include "agmen/log.hpp"

#include <cstdio>
#include <memory>
#include <mutex>

namespace agmen::log {
namespace {

struct Sink {
    std::mutex mu;
    std::shared_ptr<std::function<void(std::string_view)>> fn;
};

Sink& sink() {
    static Sink* s = new Sink();   // nunca se destruye: hilos de fondo pueden avisar al salir
    return *s;
}

}  // namespace

void write(std::string_view line) {
    std::shared_ptr<std::function<void(std::string_view)>> fn;
    {
        std::lock_guard lock(sink().mu);
        fn = sink().fn;
    }
    if (fn && *fn) {
        (*fn)(line);
        return;
    }
    std::fwrite(line.data(), 1, line.size(), stderr);
    std::fputc('\n', stderr);
    std::fflush(stderr);
}

void set_sink(std::function<void(std::string_view)> fn) {
    std::lock_guard lock(sink().mu);
    sink().fn = fn ? std::make_shared<std::function<void(std::string_view)>>(std::move(fn)) : nullptr;
}

}  // namespace agmen::log
