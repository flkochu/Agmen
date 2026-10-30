// agmen/platform.hpp — utilidades que se comportan distinto en cada sistema
// operativo y que Agmen unifica.
#pragma once

#include <chrono>

namespace agmen::platform {

/// Duerme hasta `deadline` con precisión de ~1 ms o mejor en los tres sistemas.
///
/// Por qué existe: en Windows, std::this_thread::sleep_until usa por defecto
/// la resolución del temporizador del sistema (~15.6 ms). Un lazo a 100 Hz
/// (10 ms) sería imposible. Aquí se usa un temporizador de alta resolución.
void sleep_until(std::chrono::steady_clock::time_point deadline);

/// Hace que la consola muestre bien los acentos y "µs" (UTF-8).
/// En Linux/macOS no hace nada; en Windows cambia la página de códigos.
void init_console();

}  // namespace agmen::platform
