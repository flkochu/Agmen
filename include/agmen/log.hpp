// agmen/log.hpp — a dónde van los avisos de Agmen.
//
// Por defecto, a stderr. El módulo de Python los manda a sys.stderr (así se
// ven igual en la terminal, en `agmen run` y en las pruebas).
#pragma once

#include <functional>
#include <string_view>

namespace agmen::log {

/// Escribe una línea de aviso (sin '\n' al final).
void write(std::string_view line);

/// Cambia a dónde van los avisos. Sin función = de vuelta a stderr.
void set_sink(std::function<void(std::string_view)> sink);

}  // namespace agmen::log
