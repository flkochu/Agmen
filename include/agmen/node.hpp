// agmen/node.hpp — un nodo con varias frecuencias (RFC 0006, nivel 5).
//
//   agmen::Node node("controlador");
//   auto pub = node.publisher<MotorCommand>("/cmd");
//   node.subscribe<ImuSample>("/imu", [&](const auto& s) { imu = s.message; });
//   node.every(1ms, [&] { pub.publish(controlar(imu)); });          // 1 kHz
//   auto ia = node.group("ia");                                     // otro hilo
//   node.every(100ms, [&] { plan = planear(imu); }, ia);            // si tarda, no frena al control
//   node.spin();                                                    // hasta Ctrl+C
//
// - Todo lo que no tiene grupo corre en el GRUPO PRINCIPAL (el hilo que llama
//   spin()): una función a la vez, en orden, sin candados.
// - Cada node.group("x") es OTRO HILO. Lo de grupos distintos corre al mismo
//   tiempo; para compartir datos entre grupos usa agmen::Latest<T>.
// - every() programa por deadline (ciclo k en inicio + k·periodo): el atraso
//   no se acumula. Si una función tarda de más, los ciclos perdidos se saltan,
//   se cuentan y se avisan.
// - El tiempo es el de Agmen (agmen/clock.hpp): en simulación, every() sigue
//   al tiempo simulado (más rápido, más lento o en pausa).
#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "agmen/clock.hpp"
#include "agmen/pubsub.hpp"

namespace agmen {

/// Un grupo de un Node: su propio hilo. Se crea con node.group("nombre").
struct Group {
    std::string name;   ///< "" = el grupo principal
};

/// Opciones de Node::subscribe.
struct SubscribeOptions {
    /// 1 = quedarse con el más reciente (lo correcto para sensores y comandos).
    /// N = guardar hasta N; si se llena, se descarta el más viejo.
    std::size_t queue = 1;
    Group group{};
};

/// Cuentas de un temporizador (node.timers()).
struct TimerStats {
    std::string name;              ///< "every(1 ms)"
    std::string group;
    std::int64_t period_ns = 0;
    std::uint64_t runs = 0;        ///< veces que corrió
    std::uint64_t late = 0;        ///< veces que corrió tarde (más de una décima del periodo y de 1 ms)
    std::uint64_t skipped = 0;     ///< ciclos saltados
    std::int64_t worst_late_ns = 0;
    std::int64_t worst_run_ns = 0; ///< lo más que tardó la función (tiempo real)
};

/// Cuentas de una suscripción (node.subscriptions()).
struct SubscriptionStats {
    std::string where;
    std::string type;
    std::string group;
    std::size_t queue = 1;
    std::uint64_t received = 0;    ///< mensajes que llegaron
    std::uint64_t delivered = 0;   ///< los que recibió tu función
    std::uint64_t replaced = 0;    ///< queue = 1: los que reemplazó uno más nuevo
    std::uint64_t dropped = 0;     ///< queue = N: los que se descartaron por cola llena
    std::int64_t worst_run_ns = 0;
};

/// Un "buzón" con el último valor, seguro entre hilos (para compartir datos
/// entre grupos de un Node).
template <class T>
class Latest {
public:
    void set(T value) {
        std::lock_guard lock(mu_);
        value_ = std::move(value);
        ++version_;
    }
    /// El último valor, o std::nullopt si aún no hay.
    [[nodiscard]] std::optional<T> get() const {
        std::lock_guard lock(mu_);
        return value_;
    }
    /// El último valor, o `fallback` si aún no hay.
    [[nodiscard]] T get_or(T fallback) const {
        std::lock_guard lock(mu_);
        return value_ ? *value_ : std::move(fallback);
    }
    /// Cuántas veces se ha llamado set() (para saber si hay algo nuevo).
    [[nodiscard]] std::uint64_t version() const {
        std::lock_guard lock(mu_);
        return version_;
    }

private:
    mutable std::mutex mu_;
    std::optional<T> value_;
    std::uint64_t version_ = 0;
};

/// Lo que devuelve node.publisher<M>(): se copia barato y publica con
/// t_data = node.now().
template <wire::Message M>
class NodePublisher {
public:
    explicit NodePublisher(std::shared_ptr<Publisher<M>> p) : p_(std::move(p)) {}
    net::SendStatus publish(const M& msg) const { return p_->publish(msg, clock::now_ns()); }
    net::SendStatus publish(const M& msg, std::int64_t t_data) const { return p_->publish(msg, t_data); }
    [[nodiscard]] Publisher<M>& raw() const { return *p_; }

private:
    std::shared_ptr<Publisher<M>> p_;
};

namespace detail {

// Una suscripción sin tipo, para que el executor las maneje todas igual.
class SubBase {
public:
    virtual ~SubBase() = default;
    [[nodiscard]] virtual std::intptr_t handle() const = 0;
    /// Lee lo que ya llegó al socket (sin esperar) y lo pone en la cola.
    virtual void pull() = 0;
    [[nodiscard]] virtual bool pending() const = 0;
    /// Entrega a tu función lo que hay en la cola.
    virtual void dispatch() = 0;
    virtual void heartbeat() = 0;
    [[nodiscard]] SubscriptionStats stats() const;

    std::string where, type, group;
    std::size_t queue = 1;
    std::atomic<std::uint64_t> received{0}, delivered{0}, replaced{0}, dropped{0};
    std::atomic<std::int64_t> worst_run_ns{0};
};

std::int64_t steady_now_ns();
void note_max(std::atomic<std::int64_t>& slot, std::int64_t value);

template <wire::Message M>
class Sub final : public SubBase {
public:
    using Callback = std::function<void(const Sample<M>&)>;
    Sub(const std::string& w, Callback cb, const SubscribeOptions& opt) : sub_(w), cb_(std::move(cb)) {
        where = w;
        type = std::string(M::kName);
        group = opt.group.name;
        queue = opt.queue == 0 ? 1 : opt.queue;
    }
    [[nodiscard]] std::intptr_t handle() const override { return sub_.native_handle(); }
    void pull() override {
        // Tope por vuelta: si llegan mensajes sin parar, los temporizadores
        // del grupo también tienen su turno.
        for (int i = 0; i < 1024; ++i) {
            auto s = sub_.receive(std::chrono::milliseconds(0));
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
    [[nodiscard]] bool pending() const override { return !q_.empty(); }
    void dispatch() override {
        // Se entrega lo que había al empezar: lo que llegue mientras tanto
        // espera a la siguiente vuelta.
        std::deque<Sample<M>> batch;
        batch.swap(q_);
        for (const auto& s : batch) {
            const auto t0 = steady_now_ns();
            cb_(s);
            note_max(worst_run_ns, steady_now_ns() - t0);
            ++delivered;
        }
    }
    void heartbeat() override { sub_.heartbeat(); }

private:
    Subscriber<M> sub_;
    Callback cb_;
    std::deque<Sample<M>> q_;
};

struct NodeImpl;

}  // namespace detail

class Node {
public:
    /// `name` aparece en los avisos. Si el nodo corre con `agmen run`, el hub
    /// lo conoce por el nombre de robot.toml (AGMEN_NODE).
    explicit Node(std::string name = "");
    ~Node();
    Node(const Node&) = delete;
    Node& operator=(const Node&) = delete;

    [[nodiscard]] const std::string& name() const;

    /// Un publicador de este nodo. El nodo lo mantiene vivo y late con el hub.
    template <wire::Message M>
    [[nodiscard]] NodePublisher<M> publisher(const std::string& to) {
        auto p = std::make_shared<Publisher<M>>(to);
        add_heartbeat([p] { p->heartbeat(); });
        return NodePublisher<M>(std::move(p));
    }

    /// Llama a `cb` cuando llega un M por `where` (topic o dirección). `cb`
    /// recibe `const agmen::Sample<M>&` (sobre + mensaje) o `const M&`.
    template <wire::Message M, class F>
    void subscribe(const std::string& where, F&& cb, SubscribeOptions opt = {}) {
        typename detail::Sub<M>::Callback fn;
        if constexpr (std::is_invocable_v<F&, const Sample<M>&>) {
            fn = std::forward<F>(cb);
        } else {
            static_assert(std::is_invocable_v<F&, const M&>,
                          "subscribe<M>: la función debe recibir const agmen::Sample<M>& o const M&");
            fn = [f = std::forward<F>(cb)](const Sample<M>& s) mutable { f(s.message); };
        }
        add_subscription(std::make_unique<detail::Sub<M>>(where, std::move(fn), opt));
    }

    /// Llama a `cb` cada `period` (tiempo de Agmen).
    void every(std::chrono::nanoseconds period, std::function<void()> cb, const Group& group = {});

    /// Un grupo nuevo: su propio hilo. Lo de este grupo corre al mismo tiempo
    /// que lo del grupo principal.
    [[nodiscard]] Group group(const std::string& name);

    /// Corre el nodo hasta stop(), Ctrl+C, o que `agmen run` lo detenga. Si una
    /// de tus funciones lanza una excepción, el nodo se detiene y spin() la
    /// vuelve a lanzar.
    void spin();

    /// Como spin(), pero solo durante `duration` (pruebas, demos).
    void spin_for(std::chrono::nanoseconds duration);

    /// Pide parar (se puede llamar desde cualquier hilo o función).
    void stop();

    /// La hora de Agmen (la que va en t_data).
    [[nodiscard]] std::int64_t now() const { return clock::now_ns(); }

    [[nodiscard]] std::vector<TimerStats> timers() const;
    [[nodiscard]] std::vector<SubscriptionStats> subscriptions() const;

private:
    void add_heartbeat(std::function<void()> fn);
    void add_subscription(std::unique_ptr<detail::SubBase> sub);
    void run(std::optional<std::chrono::nanoseconds> duration);

    std::unique_ptr<detail::NodeImpl> impl_;
};

}  // namespace agmen
