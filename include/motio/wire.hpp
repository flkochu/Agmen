// motio/wire.hpp — formato de cable v0 de Motio
//
// Este archivo define el contrato entre lenguajes. Cualquier nodo (C++,
// Python, un ESP32...) que escriba y lea exactamente estos bytes puede hablar
// con cualquier otro. La especificación está en docs/PROTOCOLO.md; este código
// es la implementación de referencia.
//
// Reglas del formato v0:
//  - Todo en little-endian, sin importar la arquitectura de la máquina.
//  - Header fijo de 32 bytes, seguido del payload.
//  - Cada tipo de mensaje tiene un type_id = FNV-1a 64 de su esquema. Si el
//    emisor y el receptor no coinciden, se reporta un error claro (nunca
//    fallar en silencio).
#pragma once

#include <array>
#include <bit>
#include <concepts>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace motio::wire {

/// Bytes mágicos al inicio de cada paquete: "MOT".
inline constexpr std::array<std::uint8_t, 3> kMagic{'M', 'O', 'T'};
/// Versión del formato de cable.
inline constexpr std::uint8_t kVersion = 0;
/// Tamaño del header en bytes.
inline constexpr std::size_t kHeaderLen = 32;

using Bytes = std::vector<std::uint8_t>;
using ByteView = std::span<const std::uint8_t>;

// ---------------------------------------------------------------------------
// Errores
// ---------------------------------------------------------------------------

/// Todo lo que puede salir mal al decodificar. `what()` explica exactamente
/// qué pasó, para que el usuario no tenga que adivinar.
class WireError : public std::runtime_error {
public:
    enum class Kind {
        TooShort,            ///< el paquete es más corto que el header
        BadMagic,            ///< no empieza con "MOT": no es de Motio
        UnsupportedVersion,  ///< otra versión del protocolo
        TypeMismatch,        ///< el tipo no es el que espera el receptor
        PayloadLenMismatch,  ///< payload_len no coincide con los bytes recibidos
        BadPayloadSize,      ///< el payload no mide lo que el tipo requiere
    };

    WireError(Kind kind, std::string message)
        : std::runtime_error(std::move(message)), kind_(kind) {}

    [[nodiscard]] Kind kind() const noexcept { return kind_; }

private:
    Kind kind_;
};

// ---------------------------------------------------------------------------
// type_id: hash del esquema
// ---------------------------------------------------------------------------

/// FNV-1a de 64 bits. Es trivial de implementar en cualquier lenguaje (ver
/// python/motio/wire.py), por eso lo usamos. Se calcula en compilación.
constexpr std::uint64_t fnv1a64(std::string_view data) noexcept {
    std::uint64_t hash = 0xcbf29ce484222325ULL;
    for (char c : data) {
        hash ^= static_cast<std::uint8_t>(c);
        hash *= 0x100000001b3ULL;
    }
    return hash;
}

// ---------------------------------------------------------------------------
// Lectura y escritura little-endian portables
// ---------------------------------------------------------------------------
// Se arma byte por byte con corrimientos: funciona igual en cualquier CPU
// (little o big endian) y el compilador lo optimiza a una sola instrucción.

template <std::unsigned_integral T>
void put_le(Bytes& out, T value) {
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        out.push_back(static_cast<std::uint8_t>(value >> (8 * i)));
    }
}

template <std::unsigned_integral T>
[[nodiscard]] T get_le(ByteView buf, std::size_t at) {
    T value = 0;
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        value |= static_cast<T>(static_cast<T>(buf[at + i]) << (8 * i));
    }
    return value;
}

inline void put_f32(Bytes& out, float v) { put_le(out, std::bit_cast<std::uint32_t>(v)); }

[[nodiscard]] inline float get_f32(ByteView buf, std::size_t at) {
    return std::bit_cast<float>(get_le<std::uint32_t>(buf, at));
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

/// Header fijo de 32 bytes que precede a todo mensaje.
///
/// | offset | tamaño | campo       |
/// |--------|--------|-------------|
/// | 0      | 3      | magic "MOT" |
/// | 3      | 1      | version     |
/// | 4      | 8      | type_id     |
/// | 12     | 8      | seq         |
/// | 20     | 8      | stamp_ns    |
/// | 28     | 4      | payload_len |
struct Header {
    std::uint64_t type_id = 0;
    /// Número de secuencia: el publisher lo incrementa en cada mensaje.
    std::uint64_t seq = 0;
    /// Momento de envío, en nanosegundos desde la época Unix.
    std::uint64_t stamp_ns = 0;
    std::uint32_t payload_len = 0;

    void write_to(Bytes& out) const;

    /// Lee y valida el header. Lanza WireError si algo no cuadra.
    /// Devuelve el header y la vista del payload.
    [[nodiscard]] static std::pair<Header, ByteView> read_from(ByteView buf);

    friend bool operator==(const Header&, const Header&) = default;
};

// ---------------------------------------------------------------------------
// Mensajes
// ---------------------------------------------------------------------------

/// Requisitos de un tipo de mensaje. En el nivel 3 estos tipos se van a
/// generar automáticamente desde un archivo de esquema; por ahora se escriben
/// a mano para entender qué hace el generador.
template <class M>
concept Message = requires(const M& msg, Bytes& out, ByteView payload) {
    { M::kName } -> std::convertible_to<std::string_view>;
    { M::kSchema } -> std::convertible_to<std::string_view>;
    msg.encode_payload(out);
    { M::decode_payload(payload) } -> std::same_as<M>;
};

/// Hash del esquema de M; cambia si cambia cualquier campo.
template <Message M>
inline constexpr std::uint64_t type_id_v = fnv1a64(M::kSchema);

/// Lectura de una IMU: acelerómetro (m/s²) y giroscopio (rad/s).
struct ImuSample {
    static constexpr std::string_view kName = "ImuSample";
    static constexpr std::string_view kSchema =
        "ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}";
    static constexpr std::size_t kPayloadLen = 6 * 4;

    std::array<float, 3> accel{};
    std::array<float, 3> gyro{};

    void encode_payload(Bytes& out) const;
    [[nodiscard]] static ImuSample decode_payload(ByteView payload);

    friend bool operator==(const ImuSample&, const ImuSample&) = default;
};

static_assert(Message<ImuSample>);

// ---------------------------------------------------------------------------
// Funciones de alto nivel
// ---------------------------------------------------------------------------

/// Serializa header + payload en un paquete listo para enviar.
template <Message M>
[[nodiscard]] Bytes encode(const M& msg, std::uint64_t seq, std::uint64_t stamp_ns) {
    Bytes payload;
    msg.encode_payload(payload);
    const Header header{
        .type_id = type_id_v<M>,
        .seq = seq,
        .stamp_ns = stamp_ns,
        .payload_len = static_cast<std::uint32_t>(payload.size()),
    };
    Bytes out;
    out.reserve(kHeaderLen + payload.size());
    header.write_to(out);
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
}

/// Mensaje decodificado junto con su header.
template <Message M>
struct Decoded {
    Header header;
    M message;
};

/// Arma el texto del error de tipo incompatible (definido en wire.cpp).
[[nodiscard]] std::string type_mismatch_text(std::string_view expected_name,
                                             std::uint64_t expected,
                                             std::uint64_t got);

/// Decodifica un paquete validando que sea del tipo M. Lanza WireError.
template <Message M>
[[nodiscard]] Decoded<M> decode(ByteView buf) {
    auto [header, payload] = Header::read_from(buf);
    if (header.type_id != type_id_v<M>) {
        throw WireError(WireError::Kind::TypeMismatch,
                        type_mismatch_text(M::kName, type_id_v<M>, header.type_id));
    }
    return {header, M::decode_payload(payload)};
}

/// Tiempo actual en ns desde la época Unix (reloj de pared).
///
/// Sirve para medir latencia entre procesos de la MISMA máquina. Entre
/// máquinas los relojes no están sincronizados (se resuelve más adelante).
[[nodiscard]] std::uint64_t now_ns();

}  // namespace motio::wire
