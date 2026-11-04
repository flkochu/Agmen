// agmen::Node y el reloj de Agmen (RFC 0006): frecuencias, grupos, colas,
// excepciones y el reloj de simulación (×2, pausa, nunca hacia atrás).
//
// Las pruebas de tiempo tienen márgenes amplios: el CI (sobre todo macOS)
// despierta tarde. La medición fina está en scripts/node_check.py.
#include <agmen/agmen.hpp>

#include <atomic>
#include <chrono>
#include <cstdio>
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
    node.spin_for(500ms);
    std::printf("  every(10 ms) en 0.5 s: %d veces\n", n.load());
    CHECK(n >= 35 && n <= 52);
    const auto t = node.timers();
    CHECK(t.size() == 1);
    CHECK(t[0].name == "every(10 ms)");
    CHECK(t[0].runs == static_cast<std::uint64_t>(n.load()));
}

// Lo que se prometió para el requisito de Huber: lo lento en otro grupo no
// frena al control; en el mismo grupo, sí.
void test_groups(bool same_group) {
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
    if (same_group) {
        CHECK(fast < 350);          // ~100 + lo poco que cabe entre tareas
        CHECK(t[0].skipped > 150);
    } else {
        CHECK(fast > 300);          // ideal 500 (el CI de macOS despierta tarde)
    }
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
        node.spin_for(500ms);
        std::printf("  sim ×2, every(20 ms) en 0.5 s reales: %d veces (ideal 50)\n", n.load());
        CHECK(n >= 35 && n <= 52);
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

}  // namespace

int main() {
    agmen::platform::init_console();
    std::puts("test_node");
    test_clock_msg();
    test_latest();
    test_every_rate();
    test_groups(false);
    test_groups(true);
    test_queue(1, 47811);
    test_queue(5, 47812);
    test_sample_callback(47813);
    test_exception();
    test_stop();
    test_sim_clock();
    if (g_failures != 0) {
        std::fprintf(stderr, "test_node: %d fallas\n", g_failures);
        return 1;
    }
    std::puts("test_node: todo bien");
    return 0;
}
