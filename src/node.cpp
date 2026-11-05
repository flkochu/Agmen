#include "agmen/node.hpp"

#include <algorithm>
#include <csignal>
#include <cstdio>
#include <limits>
#include <stdexcept>
#include <thread>

#include "agmen/hubproto.hpp"
#include "agmen/log.hpp"
#include "agmen/net.hpp"
#include "agmen/platform.hpp"

namespace agmen {
namespace detail {

std::int64_t steady_now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

void note_max(std::atomic<std::int64_t>& slot, std::int64_t value) {
    std::int64_t cur = slot.load(std::memory_order_relaxed);
    while (value > cur && !slot.compare_exchange_weak(cur, value, std::memory_order_relaxed)) {
    }
}

RawSub::RawSub(const std::string& w, const TypeInfo& t, Callback cb, const SubscribeOptions& opt)
    : sub_(w, t), cb_(std::move(cb)) {
    where = w;
    type = t.name;
    group = opt.group.name;
    queue = opt.queue == 0 ? 1 : opt.queue;
}

void RawSub::note_invalid(const std::string& why) {
    ++invalid;
    const auto now = std::chrono::steady_clock::now();
    if (warned_ && now - last_warn_ < std::chrono::seconds(5)) {
        ++unreported_;
        return;
    }
    const std::string first_line = why.substr(0, why.find('\n'));
    log::write("[agmen] ⚠ '" + where + "': " +
               (unreported_ > 0 ? "descarté " + std::to_string(unreported_ + 1) + " paquetes inválidos; el último: "
                                : std::string("descarté un paquete inválido: ")) +
               first_line + ". El nodo sigue.");
    warned_ = true;
    unreported_ = 0;
    last_warn_ = now;
}

void RawSub::pull() {
    // Tope por vuelta: si llegan mensajes sin parar, los temporizadores del
    // grupo también tienen su turno.
    for (int i = 0; i < 1024; ++i) {
        std::optional<RawSample> s;
        try {
            s = sub_.receive_raw(std::chrono::milliseconds(0));
        } catch (const wire::WireError& e) {
            // Que el hub rechace el topic es un error de configuración: detiene
            // al nodo con su mensaje. Un paquete mal hecho (de otro programa, o a
            // propósito) no: se descarta, se avisa y se sigue.
            if (e.kind() == wire::WireError::Kind::HubRejected) throw;
            note_invalid(e.what());
            continue;
        }
        if (!s) return;
        ++received;
        if (q_.size() >= queue) {
            q_.pop_front();
            if (queue == 1) {
                ++replaced;
            } else {
                ++dropped;
            }
        }
        q_.push_back(std::move(*s));
    }
}

void RawSub::dispatch() {
    // Se entrega lo que había al empezar: lo que llegue mientras tanto espera
    // a la siguiente vuelta.
    std::deque<RawSample> batch;
    batch.swap(q_);
    for (const auto& s : batch) {
        const auto t0 = steady_now_ns();
        try {
            cb_(s);
        } catch (const InvalidPayload& e) {
            note_invalid(e.what());   // el decodificador, no tu función: se descarta y se sigue
            continue;
        }
        note_max(worst_run_ns, steady_now_ns() - t0);
        ++delivered;
    }
}

SubscriptionStats RawSub::stats() const {
    return SubscriptionStats{
        .where = where,
        .type = type,
        .group = group,
        .queue = queue,
        .received = received.load(),
        .delivered = delivered.load(),
        .replaced = replaced.load(),
        .dropped = dropped.load(),
        .worst_run_ns = worst_run_ns.load(),
        .invalid = invalid.load(),
    };
}

namespace {

constexpr std::int64_t kMs = 1'000'000;
constexpr std::int64_t kSec = 1'000'000'000;

// "1 ms", "250 µs", "0.5 s", "2.5 ms"
std::string format_ns(std::int64_t ns) {
    char buf[32];
    const double v = static_cast<double>(ns);
    if (ns >= kSec) {
        std::snprintf(buf, sizeof buf, "%g s", v / 1e9);
    } else if (ns >= kMs) {
        std::snprintf(buf, sizeof buf, "%g ms", v / 1e6);
    } else if (ns >= 1000) {
        std::snprintf(buf, sizeof buf, "%g µs", v / 1e3);
    } else {
        std::snprintf(buf, sizeof buf, "%lld ns", static_cast<long long>(ns));
    }
    return buf;
}

std::string format_ms(std::int64_t ns) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.1f ms", static_cast<double>(ns) / 1e6);
    return buf;
}

// ---- señales: Ctrl+C, agmen run (SIGINT / CTRL_BREAK), kill (SIGTERM) ----
std::atomic<bool> g_signal{false};

extern "C" void on_signal(int sig) {
    g_signal.store(true);
    std::signal(sig, SIG_DFL);   // un segundo Ctrl+C sí mata al proceso
}

struct SignalScope {
    using Handler = void (*)(int);
    Handler old_int, old_term;
#ifdef SIGBREAK
    Handler old_break;
#endif
    SignalScope() {
        g_signal.store(false);
        old_int = std::signal(SIGINT, on_signal);
        old_term = std::signal(SIGTERM, on_signal);
#ifdef SIGBREAK
        old_break = std::signal(SIGBREAK, on_signal);
#endif
    }
    ~SignalScope() {
        std::signal(SIGINT, old_int == SIG_ERR ? SIG_DFL : old_int);
        std::signal(SIGTERM, old_term == SIG_ERR ? SIG_DFL : old_term);
#ifdef SIGBREAK
        std::signal(SIGBREAK, old_break == SIG_ERR ? SIG_DFL : old_break);
#endif
    }
    SignalScope(const SignalScope&) = delete;
    SignalScope& operator=(const SignalScope&) = delete;
};

}  // namespace

struct Timer {
    std::string label;          // "every(1 ms)"
    std::string group;
    std::int64_t period = 0;    // ns de Agmen
    std::function<void()> cb;
    std::int64_t next = 0;
    std::atomic<std::uint64_t> runs{0}, late{0}, skipped{0};
    std::atomic<std::int64_t> worst_late{0}, worst_run{0};
    // Ventana de 1 s para el aviso.
    std::uint64_t win_skipped = 0;
    std::int64_t win_worst_late = 0;
    std::int64_t last_warn = std::numeric_limits<std::int64_t>::min() / 2;
};

struct GroupRt {
    std::string name;
    std::vector<std::unique_ptr<RawSub>> subs;
    std::vector<std::unique_ptr<Timer>> timers;
    // Lo más lento del grupo en la ventana actual (para explicar los saltos).
    std::string slow_label;
    std::int64_t slow_ns = 0;
};

struct NodeImpl {
    std::string name;
    mutable std::mutex mu;
    std::vector<std::unique_ptr<GroupRt>> groups;   // [0] = principal
    std::vector<std::function<void()>> heartbeats;  // publicadores
    std::atomic<bool> stop{false};
    std::atomic<bool> running{false};
    std::mutex err_mu;
    std::exception_ptr error;
    std::optional<net::UdpSocket> stats_sock;
    std::optional<net::Address> hub_addr;

    GroupRt& group(const std::string& g) {
        for (auto& x : groups) {
            if (x->name == g) return *x;
        }
        groups.push_back(std::make_unique<GroupRt>());
        groups.back()->name = g;
        return *groups.back();
    }

    [[nodiscard]] bool should_stop() const { return stop.load() || g_signal.load(); }

    void fail(std::exception_ptr e) {
        {
            std::lock_guard lock(err_mu);
            if (!error) error = std::move(e);
        }
        stop.store(true);
    }

    void warn(Timer& t, const GroupRt& g, std::int64_t wall) {
        if (t.win_skipped == 0) return;
        if (wall - t.last_warn < 5 * kSec) return;   // a lo más cada 5 s
        t.last_warn = wall;
        const std::string where = g.name.empty() ? "grupo principal" : "grupo '" + g.name + "'";
        // slow_ns es tiempo real; el periodo, tiempo de Agmen (en simulación a ×2, 1 ms = 0.5 ms reales).
        double speed = 1.0;
        std::string unit;
        if (clock::mode() != clock::Mode::Real) {
            const auto st = clock::state();
            if (st.speed > 0) speed = st.speed;
            char b[48];
            if (clock::mode() == clock::Mode::Sim) {
                std::snprintf(b, sizeof b, " simulados (×%g)", speed);
            } else {
                std::snprintf(b, sizeof b, " de la grabación (×%g)", speed);
            }
            unit = b;
        }
        const auto period_real = static_cast<std::int64_t>(static_cast<double>(t.period) / speed);
        std::string why;
        if (g.slow_ns < period_real) {
            why = "\n" + std::string(name.size() + 4, ' ') + "Ninguna función del grupo tardó tanto (lo más lento: " +
                  format_ms(g.slow_ns) + "): el sistema despertó tarde al nodo. ¿La computadora está muy cargada?";
        } else if (!g.slow_label.empty() && g.slow_label != t.label) {
            why = "\n" + std::string(name.size() + 4, ' ') + "Lo más lento del " + where + ": " + g.slow_label +
                  " tardó " + format_ms(g.slow_ns) + ". Ponlo en otro grupo: node.group(\"lento\")";
        } else if (g.slow_label == t.label) {
            why = "\n" + std::string(name.size() + 4, ' ') + "Tu función tardó " + format_ms(g.slow_ns) +
                  " y el periodo es " + format_ns(t.period) + unit + ".";
        }
        log::write("[" + name + "] ⚠ " + t.label + ": " + std::to_string(t.win_skipped) +
                   " ciclos saltados en el último segundo (peor atraso " + format_ms(t.win_worst_late) + ", " +
                   where + ")." + why);
    }

    // Cuentas al hub (agmen node list, panel). Si el hub no existe o es viejo, no pasa nada.
    void send_stats() {
        try {
            if (!stats_sock) {
                stats_sock.emplace(net::UdpSocket::bind("127.0.0.1:0"));
                hub_addr.emplace(net::Address::resolve(hub::hub_address()));
            }
            std::string text;
            const std::string node = hub::node_name();
            const std::string pid = hub::process_id();
            std::lock_guard lock(mu);
            for (const auto& g : groups) {
                for (const auto& t : g->timers) {
                    if (!text.empty()) text += '\n';
                    text += hub::line("stat", {{"node", node},
                                               {"pid", pid},
                                               {"kind", "timer"},
                                               {"name", t->label},
                                               {"group", g->name},
                                               {"period_ns", std::to_string(t->period)},
                                               {"runs", std::to_string(t->runs.load())},
                                               {"late", std::to_string(t->late.load())},
                                               {"skipped", std::to_string(t->skipped.load())},
                                               {"worst_late_ns", std::to_string(t->worst_late.load())},
                                               {"worst_run_ns", std::to_string(t->worst_run.load())}});
                }
                for (const auto& s : g->subs) {
                    if (!text.empty()) text += '\n';
                    text += hub::line("stat", {{"node", node},
                                               {"pid", pid},
                                               {"kind", "sub"},
                                               {"name", s->where},
                                               {"type", s->type},
                                               {"group", g->name},
                                               {"queue", std::to_string(s->queue)},
                                               {"received", std::to_string(s->received.load())},
                                               {"delivered", std::to_string(s->delivered.load())},
                                               {"replaced", std::to_string(s->replaced.load())},
                                               {"dropped", std::to_string(s->dropped.load())},
                                               {"worst_run_ns", std::to_string(s->worst_run_ns.load())}});
                }
            }
            if (text.empty() || text.size() > 60000) return;
            const std::span<const std::uint8_t> bytes(reinterpret_cast<const std::uint8_t*>(text.data()),
                                                      text.size());
            (void)stats_sock->send_to(bytes, *hub_addr);
            // El hub solo contesta si no entiende "stat" (hub viejo): se descarta.
            std::uint8_t junk[512];
            while (stats_sock->recv_from(junk, std::chrono::milliseconds(0))) {
            }
        } catch (const std::exception&) {
            // Las cuentas son un extra: nunca detienen al nodo.
        }
    }

    void run_group(GroupRt& g, bool is_main, std::int64_t end_steady) {
        const platform::PromptThread prompt;   // macOS: que el hilo despierte a tiempo
        try {
            run_group_impl(g, is_main, end_steady);
        } catch (...) {
            fail(std::current_exception());
        }
    }

    void run_group_impl(GroupRt& g, bool is_main, std::int64_t end_steady) {
        using namespace std::chrono;
        // Los sockets a vigilar. Los de un tipo arrow cambian (conexiones TCP que
        // llegan y se van, RFC 0008), así que la lista se arma en cada vuelta.
        std::vector<std::intptr_t> handles;
        std::vector<std::size_t> owner;   // handles[i] es de g.subs[owner[i]]
        const auto gather = [&] {
            handles.clear();
            owner.clear();
            for (std::size_t k = 0; k < g.subs.size(); ++k) {
                for (const auto h : g.subs[k]->handles()) {
                    handles.push_back(h);
                    owner.push_back(k);
                }
            }
        };
        std::unique_ptr<bool[]> ready;
        std::size_t ready_cap = 0;

        // En simulación (y reproducción), el tiempo empieza cuando llega el primer /clock.
        while (!g.timers.empty() && clock::mode() != clock::Mode::Real && !clock::state().received) {
            if (should_stop() || (end_steady != 0 && steady_now_ns() >= end_steady)) return;
            std::this_thread::sleep_for(milliseconds(5));
        }
        const std::int64_t start = clock::mono_ns();
        for (auto& t : g.timers) t->next = start + t->period;

        std::int64_t last_beat = steady_now_ns();
        std::int64_t win_start = last_beat;

        while (!should_stop()) {
            const std::int64_t wall = steady_now_ns();
            if (end_steady != 0 && wall >= end_steady) {
                stop.store(true);
                break;
            }

            // 1. Temporizadores que ya tocan (cada uno una vez por vuelta).
            for (auto& tp : g.timers) {
                Timer& t = *tp;
                const std::int64_t now = clock::mono_ns();
                if (now < t.next) continue;
                const std::int64_t late = now - t.next;
                const std::int64_t t0 = steady_now_ns();
                t.cb();
                const std::int64_t run = steady_now_ns() - t0;
                ++t.runs;
                note_max(t.worst_run, run);
                note_max(t.worst_late, late);
                if (late > std::max<std::int64_t>(t.period / 10, kMs)) ++t.late;
                t.win_worst_late = std::max(t.win_worst_late, late);
                if (run > g.slow_ns) {
                    g.slow_ns = run;
                    g.slow_label = t.label;
                }
                // Deadline: el siguiente ciclo es un periodo después del que
                // tocaba, no de ahora. Si ya pasó más de un periodo completo,
                // esos ciclos se saltan (no se recuperan de golpe).
                t.next += t.period;
                const std::int64_t after = clock::mono_ns();
                if (after > t.next) {
                    const std::int64_t k = (after - t.next) / t.period;
                    if (k > 0) {
                        t.next += k * t.period;
                        t.skipped += static_cast<std::uint64_t>(k);
                        t.win_skipped += static_cast<std::uint64_t>(k);
                    }
                }
                if (should_stop()) break;
            }

            // 2. Mensajes que ya esperan.
            for (auto& s : g.subs) {
                if (!s->pending()) continue;
                const std::int64_t t0 = steady_now_ns();
                s->dispatch();
                const std::int64_t run = steady_now_ns() - t0;
                if (run > g.slow_ns) {
                    g.slow_ns = run;
                    g.slow_label = "subscribe(" + s->where + ")";
                }
            }

            // 3. Latidos, cuentas y avisos, una vez por segundo.
            const std::int64_t w2 = steady_now_ns();
            if (w2 - last_beat >= kSec) {
                last_beat = w2;
                for (auto& s : g.subs) s->heartbeat();
                if (is_main) {
                    std::vector<std::function<void()>> beats;
                    {
                        std::lock_guard lock(mu);
                        beats = heartbeats;
                    }
                    for (auto& b : beats) b();
                    send_stats();
                }
            }
            if (w2 - win_start >= kSec) {
                win_start = w2;
                for (auto& t : g.timers) {
                    warn(*t, g, w2);
                    t->win_skipped = 0;
                    t->win_worst_late = 0;
                }
                g.slow_label.clear();
                g.slow_ns = 0;
            }

            // 4. Esperar hasta el siguiente temporizador o a que llegue algo.
            std::int64_t wait = 100 * kMs;   // para revisar stop() y latir
            std::int64_t earliest = std::numeric_limits<std::int64_t>::max();
            for (const auto& t : g.timers) earliest = std::min(earliest, t->next);
            if (earliest != std::numeric_limits<std::int64_t>::max()) {
                const auto wu = clock::wall_until(earliest);
                if (wu == clock::kUnknown) {
                    // Pausa o simulador paso a paso: preguntar seguido.
                    wait = std::min(wait, clock::state().paused ? 20 * kMs : kMs);
                } else {
                    wait = std::min(wait, static_cast<std::int64_t>(wu.count()));
                }
            }
            if (end_steady != 0) wait = std::min(wait, end_steady - steady_now_ns());
            for (const auto& s : g.subs) {
                if (s->pending()) wait = 0;
            }
            if (wait < 0) wait = 0;

            gather();
            if (handles.size() + 1 > ready_cap) {
                ready_cap = handles.size() + 1;
                ready.reset(new bool[ready_cap]());
            }
            const std::span<bool> ready_span(ready.get(), handles.size());
            int n = 0;
            if (wait >= 2 * kMs) {
                // poll() tiene resolución de 1 ms: se despierta ~1 ms antes y
                // el último tramo se duerme con precisión.
                n = net::wait_readable(handles, milliseconds((wait - kMs) / kMs), ready_span);
            } else {
                n = net::wait_readable(handles, milliseconds(0), ready_span);
                if (n == 0 && wait > 0) {
                    platform::sleep_until(steady_clock::now() + nanoseconds(std::min<std::int64_t>(wait, 500'000)));
                }
            }
            if (n > 0) {
                std::vector<bool> pulled(g.subs.size(), false);
                for (std::size_t i = 0; i < handles.size(); ++i) {
                    if (ready[i] && !pulled[owner[i]]) {
                        pulled[owner[i]] = true;
                        g.subs[owner[i]]->pull();
                    }
                }
            }
        }
    }
};

}  // namespace detail

Node::Node(std::string name) : impl_(std::make_unique<detail::NodeImpl>()) {
    if (!name.empty()) hub::set_default_node_name(name);
    impl_->name = name.empty() ? hub::node_name() : std::move(name);
    impl_->group("");
}

Node::~Node() = default;

const std::string& Node::name() const { return impl_->name; }

void Node::add_heartbeat(std::function<void()> fn) {
    std::lock_guard lock(impl_->mu);
    impl_->heartbeats.push_back(std::move(fn));
}

void Node::subscribe_raw(const std::string& where, const TypeInfo& type, detail::RawSub::Callback cb,
                         SubscribeOptions opt) {
    if (impl_->running.load()) throw std::logic_error("Node::subscribe: llámalo antes de spin()");
    if (!cb) throw std::invalid_argument("Node::subscribe: falta la función");
    auto sub = std::make_unique<detail::RawSub>(where, type, std::move(cb), opt);
    std::lock_guard lock(impl_->mu);
    auto& g = impl_->group(sub->group);
    g.subs.push_back(std::move(sub));
}

void Node::every(std::chrono::nanoseconds period, std::function<void()> cb, const Group& group) {
    if (period.count() <= 0) throw std::invalid_argument("Node::every: el periodo debe ser mayor que cero");
    if (!cb) throw std::invalid_argument("Node::every: falta la función");
    if (impl_->running.load()) throw std::logic_error("Node::every: llámalo antes de spin()");
    auto t = std::make_unique<detail::Timer>();
    t->label = "every(" + detail::format_ns(period.count()) + ")";
    t->group = group.name;
    t->period = period.count();
    t->cb = std::move(cb);
    std::lock_guard lock(impl_->mu);
    impl_->group(group.name).timers.push_back(std::move(t));
}

Group Node::group(const std::string& name) {
    if (name.empty()) throw std::invalid_argument("Node::group: el grupo necesita nombre");
    if (impl_->running.load()) throw std::logic_error("Node::group: llámalo antes de spin()");
    std::lock_guard lock(impl_->mu);
    (void)impl_->group(name);
    return Group{name};
}

void Node::spin() { run(std::nullopt); }

void Node::spin_for(std::chrono::nanoseconds duration) { run(duration); }

void Node::stop() { impl_->stop.store(true); }

void Node::run(std::optional<std::chrono::nanoseconds> duration) {
    auto& n = *impl_;
    if (n.running.exchange(true)) throw std::logic_error("Node::spin: ya está corriendo");
    n.stop.store(false);
    {
        std::lock_guard lock(n.err_mu);
        n.error = nullptr;
    }
    const std::int64_t end = duration ? detail::steady_now_ns() + duration->count() : 0;
    {
        detail::SignalScope signals;
        platform::FineTimers fine;
        std::vector<std::thread> threads;
        for (std::size_t i = 1; i < n.groups.size(); ++i) {
            threads.emplace_back([&n, i, end] { n.run_group(*n.groups[i], false, end); });
        }
        n.run_group(*n.groups[0], true, end);
        n.stop.store(true);
        for (auto& t : threads) t.join();
    }
    n.running.store(false);
    std::exception_ptr err;
    {
        std::lock_guard lock(n.err_mu);
        err = n.error;
    }
    if (err) std::rethrow_exception(err);
}

std::vector<TimerStats> Node::timers() const {
    std::vector<TimerStats> out;
    std::lock_guard lock(impl_->mu);
    for (const auto& g : impl_->groups) {
        for (const auto& t : g->timers) {
            out.push_back(TimerStats{
                .name = t->label,
                .group = g->name,
                .period_ns = t->period,
                .runs = t->runs.load(),
                .late = t->late.load(),
                .skipped = t->skipped.load(),
                .worst_late_ns = t->worst_late.load(),
                .worst_run_ns = t->worst_run.load(),
            });
        }
    }
    return out;
}

std::vector<SubscriptionStats> Node::subscriptions() const {
    std::vector<SubscriptionStats> out;
    std::lock_guard lock(impl_->mu);
    for (const auto& g : impl_->groups) {
        for (const auto& s : g->subs) out.push_back(s->stats());
    }
    return out;
}

}  // namespace agmen
