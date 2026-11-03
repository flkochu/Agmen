// Capítulo 11 — un nodo de C++ que usa mensajes generados desde arca9.agmen.
//
// Recibe Articulaciones (lo que pide el cerebro) y responde un Estado (lo que
// "mide" el robot). Aquí no hay servos: el estado copia lo que se pidió. Lo
// usa scripts/gen_smoke.py para probar C++ generado ↔ Python con agmen.load.
//
//   ./build/bin/arca9_eco --escuchar 127.0.0.1:7600 --responder 127.0.0.1:7601 --count 20
#include "arca9.hpp"   // lo genera agmen_add_messages a partir de arca9.agmen

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>

int main(int argc, char** argv) {
    using namespace std::chrono;
    agmen::platform::init_console();
    std::string where = "/arca9/articulaciones", reply = "/arca9/estado";
    long count = 0;   // 0 = para siempre
    for (int i = 1; i + 1 < argc; i += 2) {
        const std::string a = argv[i];
        if (a == "--escuchar") where = argv[i + 1];
        else if (a == "--responder") reply = argv[i + 1];
        else if (a == "--count") count = std::atol(argv[i + 1]);
    }

    agmen::Subscriber<arca9::Articulaciones> sub(where);
    agmen::Publisher<arca9::Estado> pub(reply, "/arca9/estado");
    std::printf("[eco] Agmen %s · %s (%zu bytes) en %s → %s (%zu bytes) a %s\n", std::string(agmen::kVersion).c_str(),
                std::string(arca9::Articulaciones::kName).c_str(), arca9::Articulaciones::kPayloadLen, where.c_str(),
                std::string(arca9::Estado::kName).c_str(), arca9::Estado::kPayloadLen, reply.c_str());
    std::fflush(stdout);

    arca9::Estado estado;   // voltaje = 7.4 y modo = QUIETO: los valores por defecto del .agmen
    long answered = 0, errors = 0;
    const auto start = steady_clock::now();
    while ((count == 0 || answered < count) && steady_clock::now() - start < seconds(20)) {
        try {
            if (auto s = sub.receive(milliseconds(100))) {
                const arca9::Articulaciones& cmd = s->message;
                estado.ciclo += 1;
                estado.q_medida = cmd.q;
                estado.modo = cmd.modo;
                for (std::size_t k = 0; k < arca9::PATAS; ++k) {   // trotar: patas en diagonal
                    estado.apoyo[k] = cmd.modo != arca9::Modo::TROTAR || (k % 3 == 0) == (estado.ciclo % 2 == 0);
                }
                pub.publish(estado);
                ++answered;
                std::printf("[eco] ciclo %u · modo %s · q[0]=%.1f · kp[11]=%.2f\n", estado.ciclo,
                            std::string(arca9::to_string(cmd.modo)).c_str(), static_cast<double>(cmd.q[0]),
                            static_cast<double>(cmd.kp[11]));
                std::fflush(stdout);
            }
        } catch (const agmen::wire::WireError& e) {
            ++errors;
            std::printf("[eco] ERROR: %s\n", e.what());   // p. ej. un Modo que no existe
            std::fflush(stdout);
        }
    }
    std::printf("[eco] respondí %ld · errores %ld\n", answered, errors);
    return 0;
}
