// Tutorial, capítulo 2 — tu propio tipo de mensaje.
//
// Un mensaje de Agmen es un struct que dice:
//   1. cómo se llama           (kName)
//   2. qué campos tiene        (kSchema: de aquí sale su type_id)
//   3. cómo se convierte a bytes y de regreso (encode/decode_payload)
//
// En el nivel 4 (Agmen 0.5) este archivo lo generará una herramienta a partir
// de un esquema; por ahora lo escribimos a mano para entender qué hace.
#pragma once

#include <agmen/agmen.hpp>

#include <string>
#include <string_view>

/// Velocidad deseada de cada rueda, de -1 (reversa total) a +1 (adelante total).
struct MotorCommand {
    static constexpr std::string_view kName = "MotorCommand";
    static constexpr std::string_view kSchema = "MotorCommand{f32 left;f32 right}";
    static constexpr std::size_t kPayloadLen = 2 * 4;

    float left = 0.0f;
    float right = 0.0f;

    void encode_payload(agmen::wire::Bytes& out) const {
        agmen::wire::put_f32(out, left);
        agmen::wire::put_f32(out, right);
    }

    static MotorCommand decode_payload(agmen::wire::ByteView p) {
        if (p.size() != kPayloadLen) {
            throw agmen::wire::WireError(
                agmen::wire::WireError::Kind::BadPayloadSize,
                "payload de 'MotorCommand' debe medir 8 bytes, llegaron " + std::to_string(p.size()));
        }
        return MotorCommand{agmen::wire::get_f32(p, 0), agmen::wire::get_f32(p, 4)};
    }
};

// El compilador revisa que MotorCommand cumpla todo lo que Agmen necesita.
static_assert(agmen::wire::Message<MotorCommand>);
