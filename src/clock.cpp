#include "agmen/clock.hpp"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <thread>

#include "agmen/pubsub.hpp"

namespace agmen {

void ClockMsg::encode_payload(wire::Bytes& out) const {
    wire::put_le(out, static_cast<std::uint64_t>(t));
    wire::put_f64(out, speed);
    wire::put_le(out, static_cast<std::uint8_t>(paused ? 1 : 0));
    wire::put_padding(out, 7);
}

ClockMsg ClockMsg::decode_payload(wire::ByteView p) {
    if (p.size() != kPayloadLen) {
        throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                              wire::payload_size_text(kName, kPayloadLen, p.size()));
    }
    ClockMsg m;
    m.t = static_cast<std::int64_t>(wire::get_le<std::uint64_t>(p, 0));
    m.speed = wire::get_f64(p, 8);
    m.paused = wire::get_le<std::uint8_t>(p, 16) != 0;
    return m;
}

namespace clock {
namespace {

using steady = std::chrono::steady_clock;

std::int64_t steady_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(steady::now().time_since_epoch()).count();
}

std::int64_t system_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

struct Global {
    std::mutex mu;
    bool configured = false;
    Mode mode = Mode::Real;
    bool stepped = false;      // el tiempo lo manda un simulador: no se interpola
    bool listen = true;        // ¿escuchar /clock en un hilo?
    std::once_flag listener_started;
    // Último /clock recibido, y cuándo (reloj monótono real).
    State st{};
    std::int64_t at_wall = 0;
    std::int64_t last_returned = 0;   // para no ir nunca hacia atrás
};

Global& g() {
    static Global* instance = new Global();   // nunca se destruye: el hilo de /clock puede seguir vivo
    return *instance;
}

std::string env(const char* name) {
#ifdef _MSC_VER
    char* value = nullptr;
    std::size_t len = 0;
    std::string out;
    if (_dupenv_s(&value, &len, name) == 0 && value != nullptr) {
        out = value;
        std::free(value);
    }
    return out;
#else
    const char* v = std::getenv(name);
    return v ? std::string(v) : std::string();
#endif
}

void ensure_configured_locked(Global& s) {
    if (s.configured) return;
    s.configured = true;
    const std::string m = env("AGMEN_CLOCK");
    if (m == "sim") {
        s.mode = Mode::Sim;
        const std::string src = env("AGMEN_CLOCK_SOURCE");
        s.stepped = !src.empty() && src != "agmen";
    } else if (!m.empty() && m != "real") {
        std::fprintf(stderr, "[agmen] ⚠ AGMEN_CLOCK='%s' no existe (usa real o sim): uso el reloj real\n", m.c_str());
    }
}

void listen_forever() {
    // Un hilo por proceso, solo en simulación: escucha /clock y alimenta el reloj.
    for (;;) {
        try {
            Subscriber<ClockMsg> sub("/clock");
            for (;;) {
                if (auto s = sub.receive(std::chrono::milliseconds(200))) update(s->message);
            }
        } catch (const std::exception& e) {
            std::fprintf(stderr, "[agmen] ⚠ reloj de simulación: %s (reintento en 1 s)\n", e.what());
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
}

void start_listener_if_needed(Global& s) {
    if (s.mode != Mode::Sim || !s.listen) return;
    std::call_once(s.listener_started, [] { std::thread(listen_forever).detach(); });
}

// Tiempo de la simulación ahora (con el candado tomado).
std::int64_t sim_now_locked(Global& s) {
    std::int64_t t = s.st.t;
    if (s.st.received && !s.st.paused && !s.stepped) {
        const double dt = static_cast<double>(steady_ns() - s.at_wall) * s.st.speed;
        t += static_cast<std::int64_t>(dt);
    }
    if (t < s.last_returned) t = s.last_returned;   // nunca hacia atrás
    s.last_returned = t;
    return t;
}

}  // namespace

// 0 = aún no se sabe, 1 = real, 2 = sim. Se lee en cada mensaje: sin candado.
static std::atomic<int> g_mode_cache{0};

Mode mode() {
    if (const int c = g_mode_cache.load(std::memory_order_acquire); c != 0) {
        return c == 2 ? Mode::Sim : Mode::Real;
    }
    auto& s = g();
    bool sim = false;
    {
        std::lock_guard lk(s.mu);
        ensure_configured_locked(s);
        sim = s.mode == Mode::Sim;
    }
    if (sim) start_listener_if_needed(s);
    g_mode_cache.store(sim ? 2 : 1, std::memory_order_release);
    return sim ? Mode::Sim : Mode::Real;
}

wire::Clock domain() { return mode() == Mode::Sim ? wire::Clock::Sim : wire::Clock::Real; }

std::string_view mode_name() { return mode() == Mode::Sim ? "simulación" : "real"; }

std::int64_t now_ns() {
    if (mode() == Mode::Real) return system_ns();
    auto& s = g();
    std::lock_guard lk(s.mu);
    return sim_now_locked(s);
}

std::int64_t mono_ns() {
    if (mode() == Mode::Real) return steady_ns();
    auto& s = g();
    std::lock_guard lk(s.mu);
    return sim_now_locked(s);
}

std::chrono::nanoseconds wall_until(std::int64_t t) {
    if (mode() == Mode::Real) return std::chrono::nanoseconds(t - steady_ns());
    auto& s = g();
    std::lock_guard lk(s.mu);
    const std::int64_t now = sim_now_locked(s);
    if (t <= now) return std::chrono::nanoseconds(0);
    if (!s.st.received || s.st.paused || s.stepped || s.st.speed <= 0) return kUnknown;
    return std::chrono::nanoseconds(static_cast<std::int64_t>(static_cast<double>(t - now) / s.st.speed));
}

State state() {
    auto& s = g();
    std::lock_guard lk(s.mu);
    ensure_configured_locked(s);
    State out = s.st;
    if (s.mode == Mode::Sim) out.t = sim_now_locked(s);
    return out;
}

void configure(Mode m, bool stepped, bool listen) {
    auto& s = g();
    {
        std::lock_guard lk(s.mu);
        s.configured = true;
        s.mode = m;
        s.stepped = stepped;
        s.listen = listen;
        s.st = State{};
        s.at_wall = steady_ns();
        s.last_returned = 0;
    }
    g_mode_cache.store(m == Mode::Sim ? 2 : 1, std::memory_order_release);
    if (m == Mode::Sim) start_listener_if_needed(s);
}

void update(const ClockMsg& m) {
    auto& s = g();
    std::lock_guard lk(s.mu);
    ensure_configured_locked(s);
    s.st.t = m.t;
    s.st.speed = m.speed;
    s.st.paused = m.paused;
    s.st.received = true;
    s.at_wall = steady_ns();
}

}  // namespace clock
}  // namespace agmen
