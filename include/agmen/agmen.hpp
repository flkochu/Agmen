// agmen/agmen.hpp — cabecera principal de Agmen.
//
//   #include <agmen/agmen.hpp>
//
// Agmen es el "sistema nervioso" de un robot: mueve mensajes entre nodos de
// forma rápida, confiable y con marca de tiempo. No contiene IA, visión ni
// drivers; eso vive en paquetes opcionales.
#pragma once

#include "agmen/clock.hpp"
#include "agmen/hubproto.hpp"
#include "agmen/net.hpp"
#include "agmen/node.hpp"
#include "agmen/platform.hpp"
#include "agmen/pubsub.hpp"
#include "agmen/version.hpp"
#include "agmen/wire.hpp"
