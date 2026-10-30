// motio/motio.hpp — cabecera principal de Motio.
//
//   #include <motio/motio.hpp>
//
// Motio es el "sistema nervioso" de un robot: mueve mensajes entre nodos de
// forma rápida, confiable y con marca de tiempo. No contiene IA, visión ni
// drivers; eso vive en paquetes opcionales.
#pragma once

#include "motio/net.hpp"
#include "motio/platform.hpp"
#include "motio/wire.hpp"

namespace motio {

inline constexpr int kVersionMajor = 0;
inline constexpr int kVersionMinor = 0;
inline constexpr int kVersionPatch = 1;

}  // namespace motio
