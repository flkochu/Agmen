#include "agmen/clock.hpp"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <thread>

#include "agmen/log.hpp"
#include "agmen/platform.hpp"
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
    std::int64_t jumped = 0;          // tiempo que llegó de golpe (mono_reading); nunca baja
};

Global& g() {
    static Global* instance = new Global();   // nunca se destruye: el hilo de /clock puede seguir vivo
    return *instance;
}

void ensure_configured_locked(Global& s) {
    if (s.configured) return;
    s.configured = true;
    const std::string m = platform::env("AGMEN_CLOCK");
    if (m == "sim" || m == "replay") {
        s.mode = m == "sim" ? Mode::Sim : Mode::Replay;
        const std::string src = platform::env("AGMEN_CLOCK_SOURCE");
        s.stepped = !src.empty() && src != "agmen";
    } else if (!m.empty() && m != "real") {
        log::write("[agmen] ⚠ AGMEN_CLOCK='" + m + "' no existe (usa real, sim o replay): uso el reloj real");
    }
}

void listen_forever() {
    // Un hilo por proceso, solo en simulación o reproducción: escucha /clock y alimenta el reloj.
    for (;;) {
        try {
            Subscriber<ClockMsg> sub("/clock");
            for (;;) {
                if (auto s = sub.receive(std::chrono::milliseconds(200))) update(s->message);
            }
        } catch (const std::exception& e) {
            log::write(std::string("[agmen] ⚠ reloj (/clock): ") + e.what() + " (reintento en 1 s)");
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
}

void start_listener_if_needed(Global& s) {
    if (s.mode == Mode::Real || !s.listen) return;
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

// 0 = aún no se sabe, 1 = real, 2 = sim, 3 = replay. Se lee en cada mensaje: sin candado.
static std::atomic<int> g_mode_cache{0};

namespace {
int cache_of(Mode m) { return m == Mode::Sim ? 2 : m == Mode::Replay ? 3 : 1; }
Mode mode_of(int c) { return c == 2 ? Mode::Sim : c == 3 ? Mode::Replay : Mode::Real; }
}  // namespace

Mode mode() {
    if (const int c = g_mode_cache.load(std::memory_order_acquire); c != 0) return mode_of(c);
    auto& s = g();
    Mode m = Mode::Real;
    {
        std::lock_guard lk(s.mu);
        ensure_configured_locked(s);
        m = s.mode;
    }
    if (m != Mode::Real) start_listener_if_needed(s);
    g_mode_cache.store(cache_of(m), std::memory_order_release);
    return m;
}

wire::Clock domain() {
    const Mode m = mode();
    return m == Mode::Sim ? wire::Clock::Sim : m == Mode::Replay ? wire::Clock::Replay : wire::Clock::Real;
}

std::string_view mode_name() {
    const Mode m = mode();
    return m == Mode::Sim ? "simulación" : m == Mode::Replay ? "reproducción" : "real";
}

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

MonoReading mono_reading() {
    if (mode() == Mode::Real) return {steady_ns(), 0};
    auto& s = g();
    std::lock_guard lk(s.mu);
    return {sim_now_locked(s), s.jumped};
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
    if (s.mode != Mode::Real) out.t = sim_now_locked(s);
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
    g_mode_cache.store(cache_of(m), std::memory_order_release);
    if (m != Mode::Real) start_listener_if_needed(s);
}

void update(const ClockMsg& m) {
    auto& s = g();
    std::lock_guard lk(s.mu);
    ensure_configured_locked(s);
    // Si el reloj estaba (o queda) en pausa, no estaba corriendo: lo que avanza
    // este /clock llegó de golpe (el botón *paso*). Corriendo, la diferencia es
    // solo la corrección de la interpolación y se trata como hoy. Un simulador
    // paso a paso no interpola nunca: tampoco se cuenta (eso lo decide él).
    if (!s.stepped && (!s.st.received || s.st.paused || m.paused)) {
        const std::int64_t before = sim_now_locked(s);
        if (m.t > before) s.jumped += m.t - before;
    }
    s.st.t = m.t;
    s.st.speed = m.speed;
    s.st.paused = m.paused;
    s.st.received = true;
    s.at_wall = steady_ns();
}

}  // namespace clock
}  // namespace agmen
