// agmen::Node y el reloj de Agmen (RFC 0006): frecuencias, grupos, colas,
// excepciones y el reloj de simulación (×2, pausa, nunca hacia atrás).
//
// Las pruebas de tiempo tienen márgenes amplios: el CI (sobre todo macOS)
// despierta tarde. La medición fina está en scripts/node_check.py.
#include <agmen/agmen.hpp>
#include <agmen/msgs.hpp>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <span>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

using namespace std::chrono_literals;
using agmen::ClockMsg;

void test_clock_msg() {
    const ClockMsg m{.t = 123'456'789'000, .speed = 2.5, .paused = true};
    agmen::wire::Bytes out;
    m.encode_payload(out);
    CHECK(out.size() == ClockMsg::kPayloadLen);
    CHECK(ClockMsg::decode_payload(out) == m);
    CHECK(agmen::wire::schema_text<ClockMsg>() == "agmen/1 Clock{time t;f64 speed;bool paused}");
}

void test_every_rate() {
    agmen::Node node("prueba");
    std::atomic<int> n{0};
    node.every(10ms, [&] { ++n; });
    node.spin_for(1s);
    const auto t = node.timers();
    std::printf("  every(10 ms) en 1 s: %d veces, %llu saltados (ideal 99 + 0)\n", n.load(),
                static_cast<unsigned long long>(t[0].skipped));
    // Por deadline, corridos + saltados = los ciclos que cupieron en 1 s, aunque
    // la máquina despierte tarde (el CI de macOS a veces tarda 100 ms). Si el
    // atraso se acumulara, la suma se quedaría corta (~66 en esa máquina).
    const auto total = t[0].runs + t[0].skipped;
    CHECK(total >= 85 && total <= 101);
    CHECK(n >= 30);
    CHECK(t.size() == 1);
    CHECK(t[0].name == "every(10 ms)");
    CHECK(t[0].runs == static_cast<std::uint64_t>(n.load()));
}

// Lo que se prometió para el requisito de Huber: lo lento en otro grupo no
// frena al control; en el mismo grupo, sí. Devuelve cuántas veces corrió el de 2 ms.
int run_groups(bool same_group) {
    agmen::Node node("prueba");
    std::atomic<int> fast{0};
    node.every(2ms, [&] { ++fast; });
    const agmen::Group g = same_group ? agmen::Group{} : node.group("lento");
    node.every(50ms, [&] { std::this_thread::sleep_for(40ms); }, g);
    node.spin_for(1s);
    const auto t = node.timers();
    std::printf("  2 ms + tarea de 40 ms en %s: %d veces, %llu saltados\n",
                same_group ? "el MISMO grupo" : "OTRO grupo", fast.load(),
                static_cast<unsigned long long>(t[0].skipped));
    return fast.load();
}

void test_groups() {
    // Ideal: ~500 en otro grupo, ~100 en el mismo. Se compara un caso contra el
    // otro en la misma máquina (las del CI de macOS a veces despiertan tarde).
    const int other = run_groups(false);
    const int same = run_groups(true);
    CHECK(other > 200);
    CHECK(other > 2 * same);
}

void test_queue(std::size_t queue, int port) {
    const std::string where = "127.0.0.1:" + std::to_string(port);
    agmen::Node node("prueba");
    std::vector<std::int64_t> got;
    node.subscribe<ClockMsg>(where, [&](const ClockMsg& m) { got.push_back(m.t); }, {.queue = queue});
    agmen::Publisher<ClockMsg> pub(where, "/prueba");
    for (int i = 0; i < 10; ++i) (void)pub.publish(ClockMsg{.t = i});
    std::this_thread::sleep_for(20ms);   // que lleguen al socket antes de spin
    node.spin_for(200ms);
    const auto s = node.subscriptions();
    CHECK(s.size() == 1);
    CHECK(s[0].received == 10);
    if (queue == 1) {
        CHECK(got.size() == 1 && got[0] == 9);   // el más reciente
        CHECK(s[0].replaced == 9);
        CHECK(s[0].dropped == 0);
    } else {
        CHECK(got.size() == 5 && got.front() == 5 && got.back() == 9);   // los 5 más nuevos
        CHECK(s[0].dropped == 5);
        CHECK(s[0].replaced == 0);
    }
    CHECK(s[0].delivered == got.size());
}

void test_sample_callback(int port) {
    const std::string where = "127.0.0.1:" + std::to_string(port);
    agmen::Node node("prueba");
    std::uint64_t seq = 99;
    node.subscribe<ClockMsg>(where, [&](const auto& s) {
        seq = s.envelope.seq;
        node.stop();
    });
    agmen::Publisher<ClockMsg> pub(where, "/prueba");
    (void)pub.publish(ClockMsg{});
    node.spin_for(2s);
    CHECK(seq == 0);
}

void test_exception() {
    agmen::Node node("prueba");
    node.every(5ms, [] { throw std::runtime_error("sensor roto"); }, node.group("otro"));
    bool caught = false;
    try {
        node.spin_for(2s);
    } catch (const std::runtime_error& e) {
        caught = std::string(e.what()) == "sensor roto";
    }
    CHECK(caught);
}

void test_stop() {
    agmen::Node node("prueba");
    int n = 0;
    node.every(1ms, [&] {
        if (++n == 20) node.stop();
    });
    const auto t0 = std::chrono::steady_clock::now();
    node.spin_for(5s);
    CHECK(n == 20);
    CHECK(std::chrono::steady_clock::now() - t0 < 2s);
}

void test_latest() {
    agmen::Latest<int> box;
    CHECK(!box.get());
    CHECK(box.get_or(7) == 7);
    box.set(3);
    CHECK(box.get() == 3);
    CHECK(box.version() == 1);
}

// Al final: cambia el reloj de TODO el proceso a simulación.
void test_sim_clock() {
    namespace ck = agmen::clock;
    ck::configure(ck::Mode::Sim, /*stepped=*/false, /*listen=*/false);
    CHECK(ck::mode() == ck::Mode::Sim);
    CHECK(ck::domain() == agmen::wire::Clock::Sim);

    // ×2: un every(20 ms) simulado corre ~cada 10 ms reales.
    ck::update(ClockMsg{.t = 0, .speed = 2.0});
    {
        agmen::Node node("sim");
        std::atomic<int> n{0};
        node.every(20ms, [&] { ++n; });
        node.spin_for(1s);
        const auto t = node.timers();
        std::printf("  sim ×2, every(20 ms) en 1 s real: %d veces, %llu saltados (ideal 99 + 0)\n", n.load(),
                    static_cast<unsigned long long>(t[0].skipped));
        const auto total = t[0].runs + t[0].skipped;
        CHECK(total >= 85 && total <= 101);   // 2 s simulados
        CHECK(n >= 30);
    }
    // En pausa no corre.
    ck::update(ClockMsg{.t = ck::now_ns(), .speed = 1.0, .paused = true});
    const auto frozen = ck::now_ns();
    {
        agmen::Node node("sim");
        std::atomic<int> n{0};
        node.every(5ms, [&] { ++n; });
        node.spin_for(200ms);
        CHECK(n == 0);
    }
    CHECK(ck::now_ns() == frozen);
    // Nunca hacia atrás: un /clock "viejo" no regresa el tiempo.
    ck::update(ClockMsg{.t = frozen - 1'000'000'000, .speed = 1.0, .paused = true});
    CHECK(ck::now_ns() == frozen);
    // Paso a paso (simulador externo): solo avanza con cada /clock.
    ck::configure(ck::Mode::Sim, /*stepped=*/true, /*listen=*/false);
    ck::update(ClockMsg{.t = 1'000'000'000});
    std::this_thread::sleep_for(20ms);
    CHECK(ck::now_ns() == 1'000'000'000);
    CHECK(ck::wall_until(2'000'000'000) == ck::kUnknown);
    ck::update(ClockMsg{.t = 1'500'000'000});
    CHECK(ck::now_ns() == 1'500'000'000);
}

// RFC 0010: el modo replay sigue a /clock como la simulación (lo publica agmen
// play), pero los mensajes dicen clock = replay.
void test_replay_clock() {
    namespace ck = agmen::clock;
    ck::configure(ck::Mode::Replay, /*stepped=*/false, /*listen=*/false);
    CHECK(ck::mode() == ck::Mode::Replay);
    CHECK(ck::domain() == agmen::wire::Clock::Replay);
    CHECK(ck::mode_name() == "reproducción");
    const std::int64_t rec = 1'700'000'000'000'000'000;   // una grabación de 2023
    ck::update(ClockMsg{.t = rec, .speed = 2.0});
    std::this_thread::sleep_for(50ms);
    const auto t = ck::now_ns();
    CHECK(t > rec + 60'000'000 && t < rec + 1'000'000'000);   // ×2: unos 100 ms de grabación
    {   // lo que publica un nodo en reproducción dice clock = replay
        agmen::Subscriber<ClockMsg> sub("127.0.0.1:0");
        agmen::Publisher<ClockMsg> pub("127.0.0.1:" + std::to_string(sub.local_port()), "/x");
        (void)pub.publish(ClockMsg{});
        const auto got = sub.receive(1s);
        CHECK(got.has_value() && got->envelope.clock == agmen::wire::Clock::Replay);
    }
    ck::update(ClockMsg{.t = rec, .speed = 1.0, .paused = true});
    CHECK(ck::now_ns() >= t);                                // nunca hacia atrás
}


// Un salto del reloj (el botón *paso* del panel) no es atraso: mono_reading().jumped
// es el total de lo que llegó de golpe y nunca baja.
void test_mono_reading_real() {
    namespace ck = agmen::clock;
    ck::configure(ck::Mode::Real, /*stepped=*/false, /*listen=*/false);
    const auto a = ck::mono_reading();
    std::this_thread::sleep_for(10ms);
    const auto b = ck::mono_reading();
    CHECK(a.jumped == 0 && b.jumped == 0);
    CHECK(a.jump_from == 0 && a.jump_to == 0 && b.jump_from == 0 && b.jump_to == 0);
    CHECK(b.t > a.t);
}

void test_mono_reading_sim() {
    namespace ck = agmen::clock;
    constexpr std::int64_t kMs = 1'000'000;
    ck::configure(ck::Mode::Sim, /*stepped=*/false, /*listen=*/false);
    ck::update(ClockMsg{.t = 1'000'000'000, .speed = 1.0, .paused = true});
    const auto r0 = ck::mono_reading();
    CHECK(r0.t == 1'000'000'000);
    std::int64_t last = r0.jumped;
    // Pasos de 0.1 s en pausa: sube exactamente 100 000 000 por paso.
    for (int k = 1; k <= 5; ++k) {
        ck::update(ClockMsg{.t = 1'000'000'000 + k * 100 * kMs, .speed = 1.0, .paused = true});
        const auto r = ck::mono_reading();
        CHECK(r.t == 1'000'000'000 + k * 100 * kMs);
        CHECK(r.jumped - r0.jumped == k * 100 * kMs);
        CHECK(r.jumped >= last);
        last = r.jumped;
    }
    // Corriendo: las correcciones de la interpolación no suben `jumped`.
    ck::update(ClockMsg{.t = ck::mono_ns(), .speed = 1.0, .paused = false});
    const auto j1 = ck::mono_reading().jumped;
    for (const double speed : {1.0, 1000.0, 2.0}) {
        for (int i = 0; i < 5; ++i) {
            ck::update(ClockMsg{.t = ck::mono_ns() + 3 * kMs, .speed = speed, .paused = false});
            std::this_thread::sleep_for(5ms);
        }
        CHECK(ck::mono_reading().jumped == j1);
    }
    // Un /clock viejo (hacia atrás) en pausa no baja nada.
    ck::update(ClockMsg{.t = ck::mono_ns(), .speed = 1.0, .paused = true});
    const auto j2 = ck::mono_reading().jumped;
    ck::update(ClockMsg{.t = 1'000'000, .speed = 1.0, .paused = true});
    CHECK(ck::mono_reading().jumped >= j2);
    // Paso a paso (simulador externo): no cuenta como salto.
    ck::configure(ck::Mode::Sim, /*stepped=*/true, /*listen=*/false);
    ck::update(ClockMsg{.t = 1'000'000'000});
    const auto s0 = ck::mono_reading().jumped;
    ck::update(ClockMsg{.t = 1'500'000'000});
    ck::update(ClockMsg{.t = 2'000'000'000, .paused = true});
    CHECK(ck::mono_reading().t == 2'000'000'000);
    CHECK(ck::mono_reading().jumped == s0);
}

// Solo es salto lo que avanza un /clock cuando el estado ANTERIOR ya estaba recibido y en pausa.
// `jumped` es del proceso entero y nunca baja: se compara contra lo que valía al empezar.
void test_mono_reading_first_clock() {
    namespace ck = agmen::clock;
    for (const bool paused : {false, true}) {
        ck::configure(ck::Mode::Sim, /*stepped=*/false, /*listen=*/false);
        const auto base = ck::mono_reading().jumped;
        ck::update(ClockMsg{.t = 7'000'000'000, .speed = 1.0, .paused = paused});
        const auto r = ck::mono_reading();
        CHECK(r.t >= 7'000'000'000);
        CHECK(r.jumped == base);                  // el PRIMER /clock no es un salto
        CHECK(r.jump_from == 0 && r.jump_to == 0);
    }
    // Replay: la primera hora es la de una grabación (~1.7e18) y varios configure() seguidos no cambian nada.
    constexpr std::int64_t rec = 1'700'000'000'000'000'000;
    ck::configure(ck::Mode::Sim, false, false);
    ck::update(ClockMsg{.t = 1'000'000'000, .speed = 1.0, .paused = true});
    ck::update(ClockMsg{.t = 1'100'000'000, .speed = 1.0, .paused = true});
    const auto j0 = ck::mono_reading().jumped;
    for (int i = 0; i < 3; ++i) {
        ck::configure(ck::Mode::Replay, false, false);
        CHECK(ck::mono_reading().jumped >= j0);
    }
    const auto j1 = ck::mono_reading().jumped;
    ck::update(ClockMsg{.t = rec, .speed = 1.0, .paused = false});
    CHECK(ck::mono_reading().jumped == j1);
    ck::configure(ck::Mode::Replay, false, false);
    ck::configure(ck::Mode::Replay, false, false);
    ck::update(ClockMsg{.t = rec, .speed = 2.0, .paused = true});
    const auto r = ck::mono_reading();
    CHECK(r.t >= rec);
    CHECK(r.jumped == j1);
    CHECK(r.jump_from == 0 && r.jump_to == 0);
    ck::configure(ck::Mode::Real, false, false);
}

// Pausar con el reloj corriendo (también a ×1000) no sube `jumped`, aunque el t del mensaje se adelante.
void test_mono_reading_pause_while_running() {
    namespace ck = agmen::clock;
    constexpr std::int64_t kMs = 1'000'000;
    for (const double speed : {1.0, 1000.0}) {
        ck::configure(ck::Mode::Sim, false, false);
        const auto base = ck::mono_reading().jumped;
        ck::update(ClockMsg{.t = 1'000'000'000, .speed = speed, .paused = false});
        std::this_thread::sleep_for(30ms);
        ck::update(ClockMsg{.t = ck::mono_ns() + 50 * kMs, .speed = speed, .paused = true});
        auto r = ck::mono_reading();
        CHECK(r.jumped == base && r.jump_from == 0 && r.jump_to == 0);
        ck::update(ClockMsg{.t = r.t + 40 * kMs, .speed = speed, .paused = true});   // ahora sí: un paso
        CHECK(ck::mono_reading().jumped - base == 40 * kMs);
    }
    ck::configure(ck::Mode::Real, false, false);
}

// Un paso de 0.1 s en pausa sube exactamente 100 000 000 y deja el intervalo (t, t + 0.1 s]; dos pasos en la
// misma pausa son UN intervalo; pausa → paso → reanudar → pausa → paso deja solo el intervalo nuevo.
void test_mono_reading_interval() {
    namespace ck = agmen::clock;
    constexpr std::int64_t kMs = 1'000'000;
    ck::configure(ck::Mode::Sim, false, false);
    const auto base = ck::mono_reading().jumped;
    ck::update(ClockMsg{.t = 1'000'000'000, .speed = 1.0, .paused = true});
    ck::update(ClockMsg{.t = 1'100'000'000, .speed = 1.0, .paused = true});
    auto r = ck::mono_reading();
    CHECK(r.t == 1'100'000'000);
    CHECK(r.jumped - base == 100'000'000);
    CHECK(r.jump_from == 1'000'000'000 && r.jump_to == 1'100'000'000);
    ck::update(ClockMsg{.t = 1'200'000'000, .speed = 1.0, .paused = true});   // segundo paso, misma pausa
    r = ck::mono_reading();
    CHECK(r.jumped - base == 200'000'000);
    CHECK(r.jump_from == 1'000'000'000 && r.jump_to == 1'200'000'000);       // UN intervalo de 0.2 s
    // reanudar, correr un poco, pausar, otro paso
    ck::update(ClockMsg{.t = 1'200'000'000, .speed = 1.0, .paused = false});
    std::this_thread::sleep_for(50ms);
    const std::int64_t t_pausa = ck::mono_ns();
    ck::update(ClockMsg{.t = t_pausa, .speed = 1.0, .paused = true});
    CHECK(ck::mono_reading().jumped - base == 200'000'000);                   // reanudar y pausar no son saltos
    ck::update(ClockMsg{.t = t_pausa + 100 * kMs, .speed = 1.0, .paused = true});
    r = ck::mono_reading();
    CHECK(r.jumped - base == 300'000'000);
    CHECK(r.jump_from == t_pausa && r.jump_to == t_pausa + 100 * kMs);        // solo el intervalo nuevo
    // Con un simulador paso a paso no hay saltos ni intervalo.
    ck::configure(ck::Mode::Sim, /*stepped=*/true, false);
    const auto b2 = ck::mono_reading().jumped;
    ck::update(ClockMsg{.t = 1'000'000'000});
    ck::update(ClockMsg{.t = 2'000'000'000, .paused = true});
    ck::update(ClockMsg{.t = 2'100'000'000, .paused = true});
    r = ck::mono_reading();
    CHECK(r.t == 2'100'000'000 && r.jumped == b2 && r.jump_from == 0 && r.jump_to == 0);
    ck::configure(ck::Mode::Real, false, false);
}

// El caso de referencia: sim ×1, every(0.5 s) A y every(10 ms) B en el mismo grupo. Un paso de 0.1 s cuando a A
// no le toca; luego B tarda 400 ms de verdad y A corre ~150 ms tarde: late = 1, igual que sin el paso.
struct ResAB {
    std::uint64_t late = 0, skipped = 0, runs = 0;
    std::int64_t worst = 0;
};

ResAB run_case_ab(bool with_step) {
    namespace ck = agmen::clock;
    constexpr std::int64_t kMs = 1'000'000;
    const std::int64_t t0 = 1'000'000'000;
    ck::configure(ck::Mode::Sim, false, false);
    ck::update(ClockMsg{.t = t0, .speed = 1.0, .paused = with_step});
    std::atomic<int> a_runs{0};
    std::atomic<bool> slow_done{false};
    agmen::Node node("ab");
    node.every(500ms, [&] { ++a_runs; });
    node.every(10ms, [&] {
        if (!slow_done && ck::mono_ns() >= t0 + 250 * kMs) {
            slow_done = true;
            std::this_thread::sleep_for(400ms);
        }
    });
    std::thread spinner([&] { node.spin_for(30s); });
    if (with_step) {
        std::this_thread::sleep_for(300ms);
        ck::update(ClockMsg{.t = t0 + 100 * kMs, .speed = 1.0, .paused = true});    // el paso: a A no le toca
        std::this_thread::sleep_for(300ms);
        ck::update(ClockMsg{.t = t0 + 100 * kMs, .speed = 1.0, .paused = false});   // reanudar
    }
    const auto end = std::chrono::steady_clock::now() + 8s;
    while (a_runs < 1 && std::chrono::steady_clock::now() < end) std::this_thread::sleep_for(5ms);
    const auto ts = node.timers();
    node.stop();
    spinner.join();
    ResAB r;
    for (const auto& t : ts) {
        if (t.name == "every(500 ms)") r = {t.late, t.skipped, t.runs, t.worst_late_ns};
    }
    ck::configure(ck::Mode::Real, false, false);
    return r;
}

void test_late_after_a_jump_still_counts() {
    constexpr std::int64_t kMs = 1'000'000;
    const ResAB sin = run_case_ab(false);
    const ResAB con = run_case_ab(true);
    std::printf("  A/B sin paso: late=%llu peor=%.0f ms · con paso: late=%llu peor=%.0f ms\n",
                static_cast<unsigned long long>(sin.late), static_cast<double>(sin.worst) / 1e6, static_cast<unsigned long long>(con.late),
                static_cast<double>(con.worst) / 1e6);
    for (const ResAB& r : {sin, con}) {
        CHECK(r.runs >= 1);
        CHECK(r.late == 1);
        CHECK(r.worst >= 80 * kMs && r.worst <= 300 * kMs);   // ~150 ms
    }
    CHECK(std::llabs(con.worst - sin.worst) < 100 * kMs);
}

// every(10 ms) con el reloj en pausa y 5 pasos de 100 ms: corre 2 veces por paso
// y no se cuenta como saltado ni como tarde. Después, con una función lenta, sí.
void test_jump_is_not_late() {
    namespace ck = agmen::clock;
    constexpr std::int64_t kMs = 1'000'000;
    ck::configure(ck::Mode::Sim, /*stepped=*/false, /*listen=*/false);
    std::int64_t t = 1'000'000'000;
    ck::update(ClockMsg{.t = t, .speed = 1.0, .paused = true});
    std::atomic<int> n{0};
    std::atomic<bool> slow{false};
    agmen::Node node("pasos");
    node.every(10ms, [&] {
        ++n;
        if (slow) std::this_thread::sleep_for(30ms);
    });
    std::thread spinner([&] { node.spin_for(4s); });
    std::this_thread::sleep_for(300ms);
    CHECK(n == 0);
    int per_step_bad = 0;
    for (int k = 0; k < 5; ++k) {
        const int before = n;
        t += 100 * kMs;
        ck::update(ClockMsg{.t = t, .speed = 1.0, .paused = true});
        std::this_thread::sleep_for(300ms);
        if (n - before != 2) ++per_step_bad;
    }
    auto st = node.timers()[0];
    std::printf("  5 pasos de 0.1 s en pausa: %d veces, %llu saltados, %llu tarde\n", n.load(),
                static_cast<unsigned long long>(st.skipped), static_cast<unsigned long long>(st.late));
    CHECK(per_step_bad == 0);
    CHECK(st.runs == 10);
    CHECK(st.skipped == 0);
    CHECK(st.late == 0);
    CHECK(st.worst_late_ns == 0);
    // Reanuda con una función de 30 ms: eso SÍ es atraso.
    slow = true;
    ck::update(ClockMsg{.t = t, .speed = 1.0, .paused = false});
    std::this_thread::sleep_for(800ms);
    node.stop();
    spinner.join();
    st = node.timers()[0];
    std::printf("  después, con una función de 30 ms: %llu saltados\n", static_cast<unsigned long long>(st.skipped));
    CHECK(st.skipped > 5);
}

// Con reloj real, una función lenta da saltados como siempre.
void test_real_slow_function() {
    namespace ck = agmen::clock;
    ck::configure(ck::Mode::Real, /*stepped=*/false, /*listen=*/false);
    agmen::Node node("real");
    node.every(10ms, [] { std::this_thread::sleep_for(30ms); });
    node.spin_for(600ms);
    const auto st = node.timers()[0];
    CHECK(st.skipped > 5);
}

// Un anuncio de esquema con miles de structs anidados, en un solo paquete.
agmen::wire::Bytes nested_announcement(int levels, const std::string& topic) {
    namespace w = agmen::wire;
    std::string body = "u8 x";
    for (int k = 0; k < levels; ++k) body = "S" + std::to_string(k) + "{" + body + "} f";
    const std::string text = "agmen/1 Raiz{" + body + "}";
    const std::string payload = topic + "\n" + text;
    const w::Envelope env{.encoding = w::Encoding::Announcement,
                          .schema_id = w::fnv1a64(text),
                          .topic_id = w::topic_id(topic),
                          .source_id = 7,
                          .payload_len = static_cast<std::uint32_t>(payload.size())};
    w::Bytes out;
    env.write_to(out);
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
}

void send_all(agmen::net::TcpStream& tcp, std::span<const std::uint8_t> data) {
    const auto end = std::chrono::steady_clock::now() + 2s;
    while (!data.empty() && std::chrono::steady_clock::now() < end) {
        const auto n = tcp.send_some(data);
        if (n < 0) return;
        data = data.subspan(static_cast<std::size_t>(n));
        if (n == 0) std::this_thread::sleep_for(1ms);
    }
}

// Lo del PR #2: un paquete mal hecho no detiene al nodo; se descarta, se
// cuenta y se avisa. Antes, un anuncio con miles de structs anidados tumbaba
// al proceso (se acababa la pila), y cualquier WireError de una suscripción
// detenía spin(). El Node corre en un std::thread: en Windows su pila es la
// normal de 1 MB, donde 300 niveles ya bastaban para tumbarlo.
void test_invalid_packets(int port) {
    namespace w = agmen::wire;
    using agmen::msgs::Imagen;
    using agmen::wire::ImuSample;
    const std::string udp_at = "127.0.0.1:" + std::to_string(port);
    const std::string tcp_at = "127.0.0.1:" + std::to_string(port + 1);
    agmen::Node node("prueba");
    std::atomic<int> imus{0}, images{0};
    node.subscribe<ImuSample>(udp_at, [&](const ImuSample&) { ++imus; }, {.queue = 64});
    node.subscribe<Imagen>(tcp_at, [&](const Imagen& m) { images += m.width == 4 ? 1 : 0; }, {.queue = 64});
    std::atomic<bool> stopped_by_error{false};
    std::string error;
    std::thread spinner([&] {
        try {
            node.spin_for(3s);
        } catch (const std::exception& e) {
            error = e.what();
            stopped_by_error = true;
        }
    });

    // 1. Por UDP: anuncios con 300 y 2500 niveles, basura y un sobre de otro tipo.
    auto udp = agmen::net::UdpSocket::bind("127.0.0.1:0");
    udp.connect(udp_at);
    std::this_thread::sleep_for(50ms);
    for (const int levels : {300, 2500}) (void)udp.send(nested_announcement(levels, "/t"));
    const std::vector<std::uint8_t> junk(100, 0xAB);
    (void)udp.send(junk);
    // 2. Por TCP: un Imagen con el sobre correcto y un payload arrow que no se puede leer.
    auto tcp = agmen::net::TcpStream::connect(agmen::net::Address::resolve(tcp_at), 2s);
    const std::vector<std::uint8_t> bad_arrow(256, 0x5A);
    const w::Envelope env{.encoding = w::Encoding::Arrow,
                          .schema_id = w::schema_id_v<Imagen>,
                          .source_id = 8,
                          .payload_len = static_cast<std::uint32_t>(bad_arrow.size())};
    w::Bytes packet;
    env.write_to(packet);
    packet.insert(packet.end(), bad_arrow.begin(), bad_arrow.end());
    send_all(tcp, packet);
    std::this_thread::sleep_for(200ms);

    // 3. Y luego mensajes buenos: deben llegar todos.
    agmen::Publisher<ImuSample> imu_pub(udp_at, "/t");
    agmen::Publisher<Imagen> img_pub(tcp_at, "/camara");
    const Imagen img{.width = 4, .height = 1, .encoding = "mono8", .step = 4, .data = {1, 2, 3, 4}};
    for (int i = 0; i < 20 && imus < 5; ++i) {
        (void)imu_pub.publish(ImuSample{});
        (void)img_pub.publish(img);
        std::this_thread::sleep_for(20ms);
    }
    std::this_thread::sleep_for(300ms);
    node.stop();
    spinner.join();

    const auto subs = node.subscriptions();
    std::uint64_t invalid = 0;
    for (const auto& s : subs) invalid += s.invalid;
    std::printf("  paquetes inválidos: %llu descartados; luego llegaron %d ImuSample y %d Imagen%s%s\n",
                static_cast<unsigned long long>(invalid), imus.load(), images.load(),
                stopped_by_error ? "; spin() falló: " : "", error.c_str());
    CHECK(!stopped_by_error);
    CHECK(imus >= 5);
    CHECK(images >= 1);
    CHECK(invalid >= 4);   // 2 anuncios, la basura y el arrow ilegible
}

}  // namespace

int main() {
    agmen::platform::init_console();
    std::puts("test_node");
    test_clock_msg();
    test_latest();
    test_mono_reading_real();
    test_real_slow_function();
    test_every_rate();
    test_groups();
    test_queue(1, 47811);
    test_queue(5, 47812);
    test_sample_callback(47813);
    test_invalid_packets(47814);   // y 47815
    test_exception();
    test_stop();
    test_sim_clock();
    test_replay_clock();
    test_mono_reading_sim();
    test_mono_reading_first_clock();
    test_mono_reading_pause_while_running();
    test_mono_reading_interval();
    test_jump_is_not_late();
    test_late_after_a_jump_still_counts();
    if (g_failures != 0) {
        std::fprintf(stderr, "test_node: %d fallas\n", g_failures);
        return 1;
    }
    std::puts("test_node: todo bien");
    return 0;
}
