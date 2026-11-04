// agmen/platform.hpp — utilidades que se comportan distinto en cada sistema
// operativo y que Agmen unifica.
#pragma once

#include <chrono>
#include <string>

namespace agmen::platform {

/// Duerme hasta `deadline` con precisión de ~1 ms o mejor en los tres sistemas.
///
/// Por qué existe: en Windows, std::this_thread::sleep_until usa por defecto
/// la resolución del temporizador del sistema (~15.6 ms). Un lazo a 100 Hz
/// (10 ms) sería imposible. Aquí se usa un temporizador de alta resolución.
void sleep_until(std::chrono::steady_clock::time_point deadline);

/// Mientras exista, pide al sistema temporizadores de 1 ms (Windows: por
/// defecto, esperar un socket o dormir tiene ~15.6 ms de resolución). En
/// Linux y macOS no hace nada. Lo usa agmen::Node::spin().
class FineTimers {
public:
    FineTimers();
    ~FineTimers();
    FineTimers(const FineTimers&) = delete;
    FineTimers& operator=(const FineTimers&) = delete;
};

/// Mientras exista, pide que ESTE hilo despierte a tiempo. En macOS, el
/// sistema agrupa los despertares de los hilos "normales" para ahorrar
/// energía (se atrasan 1-2 ms): aquí se le da al hilo la clase de servicio
/// "interactiva", la de menor holgura. En Windows y Linux no hace nada. Lo usa
/// cada grupo de un agmen::Node.
class PromptThread {
public:
    PromptThread();
    ~PromptThread();
    PromptThread(const PromptThread&) = delete;
    PromptThread& operator=(const PromptThread&) = delete;

private:
    int previous_ = -1;
};

/// Hace que la consola muestre bien los acentos y "µs" (UTF-8).
/// En Linux/macOS no hace nada; en Windows cambia la página de códigos.
void init_console();

/// El valor de la variable de entorno `name`, o "" si no existe.
///
/// Por qué existe: MSVC marca std::getenv como inseguro (C4996, que el CI
/// trata como error) y pide _dupenv_s. Todo el núcleo lee el entorno aquí.
std::string env(const char* name);

}  // namespace agmen::platform
