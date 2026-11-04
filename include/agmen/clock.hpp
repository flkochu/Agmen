// agmen/clock.hpp — el reloj de Agmen (RFC 0006, nivel 5).
//
// Regla 5: los nodos usan el reloj de Agmen, nunca el del sistema operativo.
// Así el mismo código corre en el robot real y en una simulación que va más
// rápido, más lento o en pausa.
//
//   agmen::clock::now_ns()    la hora de Agmen (la que va en t_data)
//   agmen::clock::mode()      Real o Sim
//
// El modo lo decide `agmen run` para TODO el robot ([clock] en robot.toml) y
// se lo pasa a cada nodo en AGMEN_CLOCK=real|sim (y AGMEN_CLOCK_SOURCE). En
// simulación, el tiempo llega por el topic /clock (ClockMsg):
//
//   - source = "agmen": lo publica agmen run unas 50 veces por segundo; entre
//     un mensaje y otro el reloj avanza solo, a la velocidad indicada.
//   - source = "<tu simulador>": solo avanza cuando llega un /clock (paso a paso).
//
// El reloj nunca va hacia atrás.
#pragma once

#include <chrono>
#include <cstdint>
#include <string>
#include <string_view>

#include "agmen/wire.hpp"

namespace agmen {

/// El tiempo de la simulación, por el topic /clock.
struct ClockMsg {
    static constexpr std::string_view kName = "Clock";
    static constexpr std::string_view kSchema = "Clock{time t;f64 speed;bool paused}";
    static constexpr std::size_t kPayloadLen = 24;

    std::int64_t t = 0;      ///< tiempo de la simulación, en ns
    double speed = 1.0;      ///< 2.0 = el doble de rápido que el tiempo real
    bool paused = false;

    void encode_payload(wire::Bytes& out) const;
    [[nodiscard]] static ClockMsg decode_payload(wire::ByteView p);
    friend bool operator==(const ClockMsg&, const ClockMsg&) = default;
};
static_assert(wire::Message<ClockMsg>);
static_assert(wire::compact_size_v<ClockMsg> == ClockMsg::kPayloadLen);

namespace clock {

enum class Mode { Real, Sim };

/// El modo de este proceso (AGMEN_CLOCK; por defecto Real).
[[nodiscard]] Mode mode();

/// El dominio que va en el campo `clock` de cada mensaje.
[[nodiscard]] wire::Clock domain();

/// "real" / "simulación".
[[nodiscard]] std::string_view mode_name();

/// La hora de Agmen, en ns. Real: desde la época Unix. Sim: el tiempo de la simulación.
[[nodiscard]] std::int64_t now_ns();

/// Un reloj que nunca salta, para medir y programar: en Real, el reloj
/// monótono de la computadora; en Sim, el tiempo de la simulación.
[[nodiscard]] std::int64_t mono_ns();

/// Cuánto tiempo REAL falta para que mono_ns() llegue a `t`. Si el tiempo
/// está en pausa o avanza paso a paso (simulador externo), devuelve
/// `unknown` (quien espera debe volver a preguntar en un rato).
inline constexpr std::chrono::nanoseconds kUnknown{-1};
[[nodiscard]] std::chrono::nanoseconds wall_until(std::int64_t t);

/// Estado de la simulación (lo que trae /clock).
struct State {
    std::int64_t t = 0;
    double speed = 1.0;
    bool paused = true;
    bool received = false;   ///< ¿ya llegó algún /clock?
};
[[nodiscard]] State state();

/// Para pruebas y para quien publica /clock en el mismo proceso: cambia el
/// modo sin leer el entorno, y alimenta el reloj sin escuchar /clock.
void configure(Mode mode, bool stepped = false, bool listen = true);
void update(const ClockMsg& m);

}  // namespace clock
}  // namespace agmen
