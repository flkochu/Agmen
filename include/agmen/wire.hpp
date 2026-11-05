// agmen/wire.hpp — formato de mensajes v1 de Agmen (RFC 0001)
//
// Este archivo define el contrato entre lenguajes. Cualquier nodo (C++,
// Python, un ESP32...) que escriba y lea exactamente estos bytes puede hablar
// con cualquier otro. La especificación está en docs/PROTOCOLO.md; este código
// es la implementación de referencia, revisada contra los mismos vectores de
// prueba que la de Python (tests/vectors/v1.txt).
//
// Cada mensaje = sobre de 64 bytes + payload:
//  - Todo en little-endian, sin importar la arquitectura de la máquina.
//  - El sobre lleva qué tipo es (schema_id), de qué topic, quién lo mandó,
//    su número de secuencia y dos marcas de tiempo (t_data y t_pub).
//  - El payload `compact` es el layout de un struct de C.
//  - schema_id = FNV-1a 64 del texto canónico "agmen/1 " + kSchema. Si emisor
//    y receptor no coinciden, se reporta un error claro (nunca fallar en
//    silencio).
#pragma once

#include <algorithm>
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

namespace agmen::wire {

/// Bytes mágicos al inicio de cada paquete: "AGM".
inline constexpr std::array<std::uint8_t, 3> kMagic{'A', 'G', 'M'};
/// Versión del formato de cable.
inline constexpr std::uint8_t kVersion = 1;
/// Tamaño del sobre en bytes (v1).
inline constexpr std::size_t kEnvelopeLen = 64;
/// Prefijo del texto canónico de un esquema.
inline constexpr std::string_view kSchemaPrefix = "agmen/1 ";

using Bytes = std::vector<std::uint8_t>;
using ByteView = std::span<const std::uint8_t>;

/// Cómo viene codificado el payload.
enum class Encoding : std::uint8_t {
    None = 0,          ///< sin payload
    Compact = 1,       ///< struct de C (RFC 0001 §4)
    Arrow = 2,         ///< RecordBatch de Arrow IPC (RFC 0008; agmen/arrow.hpp)
    Announcement = 3,  ///< anuncio de esquema: "topic\nesquema" (RFC 0001, anexo A)
};

/// Dominio del reloj de las marcas de tiempo.
enum class Clock : std::uint8_t { Real = 0, Sim = 1, Replay = 2 };

inline constexpr std::uint8_t kFlagShm = 0x01;  ///< payload en memoria compartida (nivel 8)
inline constexpr std::uint8_t kFlagFin = 0x02;  ///< último mensaje del stream o episodio

// ---------------------------------------------------------------------------
// Errores
// ---------------------------------------------------------------------------

/// Todo lo que puede salir mal al decodificar. `what()` explica exactamente
/// qué pasó, para que el usuario no tenga que adivinar.
class WireError : public std::runtime_error {
public:
    enum class Kind {
        TooShort,            ///< el paquete es más corto que el sobre
        BadMagic,            ///< no empieza con "AGM": no es de Agmen
        UnsupportedVersion,  ///< otra versión del protocolo (ej. v0 de Agmen ≤ 0.3)
        BadHeader,           ///< header_len inválido
        UnknownEncoding,     ///< encoding o flags desconocidos
        Unsupported,         ///< memoria compartida (nivel 8) o una parte de Arrow que Agmen no lee
        BadArrow,            ///< payload arrow mal formado o que no coincide con el esquema
        TooBig,              ///< el mensaje pasa del máximo (64 MiB o AGMEN_MAX_MESSAGE_MB)
        TypeMismatch,        ///< el tipo no es el que espera el receptor
        PayloadLenMismatch,  ///< payload_len no coincide con los bytes recibidos
        BadPayloadSize,      ///< el payload no mide lo que el tipo requiere
        NotData,             ///< es un anuncio de esquema, no datos
        BadAnnouncement,     ///< anuncio de esquema mal formado
        HubRejected,         ///< el hub rechazó el registro (ej. choque de tipos en un topic)
        BadValue,            ///< un campo trae un valor inválido (ej. un enum que no existe)
    };

    WireError(Kind kind, std::string message)
        : std::runtime_error(std::move(message)), kind_(kind) {}

    [[nodiscard]] Kind kind() const noexcept { return kind_; }

private:
    Kind kind_;
};

// ---------------------------------------------------------------------------
// Hash FNV-1a de 64 bits (schema_id, topic_id)
// ---------------------------------------------------------------------------

/// Trivial de implementar en cualquier lenguaje (ver python/agmen/schema.py).
/// Se calcula en compilación. `seed` permite continuar un hash ya empezado.
constexpr std::uint64_t fnv1a64(std::string_view data,
                                std::uint64_t seed = 0xcbf29ce484222325ULL) noexcept {
    std::uint64_t hash = seed;
    for (char c : data) {
        hash ^= static_cast<std::uint8_t>(c);
        hash *= 0x100000001b3ULL;
    }
    return hash;
}

/// topic_id = FNV-1a 64 del nombre del topic ("/imu"); 0 si no hay topic.
constexpr std::uint64_t topic_id(std::string_view topic) noexcept {
    return topic.empty() ? 0 : fnv1a64(topic);
}

// ---------------------------------------------------------------------------
// Esquemas: validación en compilación del texto canónico
// ---------------------------------------------------------------------------

namespace schema {

// Límites de un esquema (RFC 0001, anexo A). Son los mismos en C++
// (este revisor y agmen::schema::Layout), Python (agmen.schema) y JavaScript
// (agmen.js): un esquema que uno acepta lo aceptan todos. Los esquemas también
// llegan de la red, en los anuncios, y sin límites un anuncio de pocos KB
// bastaba para tumbar al receptor.

/// N de un arreglo T[N]: de 1 a 1048576.
inline constexpr std::size_t kMaxArray = std::size_t{1} << 20;
/// N de tensor<T,N>: de 1 a 32 dimensiones.
inline constexpr std::size_t kMaxTensorDims = 32;
/// Niveles de structs y arreglos anidados: cada struct y cada [N] o [] es un
/// nivel. `Pose{Vec3{f64 x} p}` usa 2 y `A{f32[2][3] m}`, 3.
inline constexpr std::size_t kMaxDepth = 32;
/// Lo más que mide un tipo de tamaño fijo (y por tanto un mensaje compact):
/// 2 GiB - 1. Para datos más grandes: bytes, T[] o tensor<T,N>.
inline constexpr std::uint64_t kMaxFixedSize = 0x7FFFFFFF;

/// Resultado de revisar un esquema: si es canónico y cuánto mide en compact.
struct Check {
    bool ok = false;
    std::size_t size = 0;      ///< tamaño del struct compact (si !variable)
    std::size_t align = 1;     ///< alineación del struct
    std::size_t error_at = 0;  ///< posición del primer error (si !ok)
    bool variable = false;     ///< tiene string, bytes, T[] o tensor: viaja en arrow (RFC 0008)
};

namespace detail {

struct Parser {
    std::string_view s;
    std::size_t i = 0;
    bool ok = true;
    std::size_t err = 0;
    std::size_t depth = 0;  ///< structs abiertos (la recursión no pasa de kMaxDepth)

    constexpr void fail() {
        if (ok) err = i;
        ok = false;
    }
    [[nodiscard]] constexpr char peek() const { return i < s.size() ? s[i] : '\0'; }
    constexpr void expect(char c) {
        if (peek() == c) {
            ++i;
        } else {
            fail();
        }
    }
    constexpr std::string_view ident() {
        const std::size_t start = i;
        const auto alpha = [](char c) {
            return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
        };
        const auto digit = [](char c) { return c >= '0' && c <= '9'; };
        if (!alpha(peek())) {
            fail();
            return {};
        }
        while (alpha(peek()) || digit(peek())) ++i;
        return s.substr(start, i - start);
    }
};

struct SizeAlign {
    std::uint64_t size = 0;  ///< hasta kMaxFixedSize + 1 (más que eso ya es error)
    std::size_t align = 1;
    bool variable = false;
    std::size_t height = 0;  ///< niveles de structs y arreglos (un primitivo: 0)
};

constexpr std::size_t primitive_size(std::string_view n) {
    if (n == "bool" || n == "i8" || n == "u8") return 1;
    if (n == "i16" || n == "u16") return 2;
    if (n == "i32" || n == "u32" || n == "f32") return 4;
    if (n == "i64" || n == "u64" || n == "f64" || n == "time" || n == "duration") return 8;
    return 0;
}

constexpr std::uint64_t align_up(std::uint64_t n, std::size_t a) { return (n + a - 1) / a * a; }

/// ¿`name` ya es el nombre de un campo en `fields`? `fields` es el texto de
/// los campos ya leídos de un struct (canónico: "T a;U b;..."), así que cada
/// nombre es lo que hay entre el espacio y un ';' fuera de las llaves. Sin
/// guardar los nombres: no hay límite de campos y la pila no crece.
constexpr bool has_field(std::string_view fields, std::string_view name) {
    std::size_t depth = 0;
    for (std::size_t k = 0; k < fields.size(); ++k) {
        const char c = fields[k];
        if (c == '{') {
            ++depth;
        } else if (c == '}') {
            --depth;
        } else if (c == ';' && depth == 0) {
            std::size_t b = k;
            while (b > 0 && fields[b - 1] != ' ') --b;
            if (fields.substr(b, k - b) == name) return true;
        }
    }
    return false;
}

constexpr SizeAlign parse_struct(Parser& p);

constexpr SizeAlign parse_type(Parser& p) {
    const std::size_t start = p.i;
    const std::string_view name = p.ident();
    if (!p.ok) return {};
    SizeAlign t;
    if (p.peek() == '{') {  // struct anidado
        p.i = start;
        t = parse_struct(p);
        if (!p.ok) return {};
    } else if (name == "string" || name == "bytes") {
        t = {0, 1, true, 0};
    } else if (name == "tensor") {  // tensor<T,N>, T numérico, N de 1 a 32
        p.expect('<');
        const std::string_view e = p.ident();
        if (!p.ok || primitive_size(e) == 0 || e == "bool" || e == "time" || e == "duration") {
            p.fail();
            return {};
        }
        p.expect(',');
        if (p.peek() < '1' || p.peek() > '9') {
            p.fail();
            return {};
        }
        std::size_t n = 0;
        while (p.peek() >= '0' && p.peek() <= '9') {
            n = n * 10 + static_cast<std::size_t>(p.peek() - '0');
            if (n > kMaxTensorDims) {  // aquí, antes de que el número dé la vuelta
                p.fail();
                return {};
            }
            ++p.i;
        }
        p.expect('>');
        t = {0, 1, true, 0};
    } else {
        const std::size_t sz = primitive_size(name);
        if (sz == 0) {
            p.i = start;
            p.fail();
            return {};
        }
        t = {sz, sz, false, 0};
    }
    // T[N], T[N][M], T[]...: se leen todas y se arman de derecha a izquierda,
    // como en Layout: f32[2][3] = 2 arreglos de f32[3]; f32[][3] = lista de f32[3].
    std::array<std::size_t, kMaxDepth> dims{};  // 0 = T[]
    std::size_t count = 0;
    while (p.ok && p.peek() == '[') {
        if (t.height + count + 1 > kMaxDepth) {
            p.fail();
            return {};
        }
        ++p.i;
        if (p.peek() == ']') {  // T[]: tamaño variable
            ++p.i;
            dims[count++] = 0;
            continue;
        }
        if (p.peek() == '0' || p.peek() < '0' || p.peek() > '9') {
            p.fail();
            return {};
        }
        std::size_t n = 0;
        while (p.peek() >= '0' && p.peek() <= '9') {
            n = n * 10 + static_cast<std::size_t>(p.peek() - '0');
            ++p.i;
            if (n > kMaxArray) {
                p.fail();
                return {};
            }
        }
        p.expect(']');
        dims[count++] = n;
    }
    for (std::size_t k = count; k-- > 0;) {
        if (dims[k] == 0) {
            t.variable = true;
        } else if (!t.variable) {
            if (t.size > kMaxFixedSize / dims[k]) {  // el arreglo pasaría de 2 GiB
                p.fail();
                return {};
            }
            t.size *= dims[k];
        }
    }
    t.height += count;
    return t;
}

constexpr SizeAlign parse_struct(Parser& p) {
    if (++p.depth > kMaxDepth) {
        p.fail();
        return {};
    }
    (void)p.ident();
    p.expect('{');
    const std::size_t fields_at = p.i;
    SizeAlign st{0, 1, false, 0};
    while (p.ok) {
        const SizeAlign f = parse_type(p);
        p.expect(' ');
        const std::size_t name_at = p.i;
        const std::string_view fname = p.ident();
        if (!p.ok) break;
        if (has_field(p.s.substr(fields_at, name_at - fields_at), fname)) {  // campo repetido
            p.i = name_at;
            p.fail();
            break;
        }
        if (f.variable) st.variable = true;
        // Sin desbordes: cada campo mide como mucho kMaxFixedSize y la suma se
        // detiene en kMaxFixedSize + 1. Solo cuenta si al final el struct es fijo.
        st.size = align_up(st.size, f.align) + f.size;
        if (st.size > kMaxFixedSize) st.size = kMaxFixedSize + 1;
        st.align = f.align > st.align ? f.align : st.align;
        if (f.height + 1 > st.height) st.height = f.height + 1;
        if (p.peek() == ';') {
            ++p.i;
        } else {
            p.expect('}');
            break;
        }
    }
    --p.depth;
    st.size = align_up(st.size, st.align);
    if (p.ok && (st.height > kMaxDepth || (!st.variable && st.size > kMaxFixedSize))) p.fail();
    return st;
}

}  // namespace detail

/// Revisa que `body` (sin el prefijo "agmen/1 ") sea un esquema canónico:
/// tipos f32/u8/..., un solo espacio entre tipo y nombre, ';' entre campos y
/// sin ';' final. Ej.: "MotorCommand{f32 left;f32 right}". Con los límites de
/// arriba (kMaxDepth, kMaxFixedSize...).
constexpr Check check(std::string_view body) {
    detail::Parser p{body};
    const detail::SizeAlign st = detail::parse_struct(p);
    if (p.ok && p.i != body.size()) p.fail();  // sobra texto
    if (!p.ok) return Check{false, 0, 1, p.err, false};
    if (st.variable) return Check{true, 0, 1, 0, true};
    return Check{true, static_cast<std::size_t>(st.size), st.align, 0, false};
}

}  // namespace schema

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
inline void put_f64(Bytes& out, double v) { put_le(out, std::bit_cast<std::uint64_t>(v)); }
/// Relleno del layout compact: siempre ceros (grabaciones y hashes deterministas).
inline void put_padding(Bytes& out, std::size_t n) { out.insert(out.end(), n, std::uint8_t{0}); }

/// Un std::array con todos sus elementos iguales a `v` (lo usa el código que
/// genera `agmen gen` para los valores por defecto de los arreglos).
template <class T, std::size_t N>
[[nodiscard]] constexpr std::array<T, N> filled(const T& v) {
    std::array<T, N> a{};
    a.fill(v);
    return a;
}

[[nodiscard]] inline float get_f32(ByteView buf, std::size_t at) {
    return std::bit_cast<float>(get_le<std::uint32_t>(buf, at));
}
[[nodiscard]] inline double get_f64(ByteView buf, std::size_t at) {
    return std::bit_cast<double>(get_le<std::uint64_t>(buf, at));
}

// ---------------------------------------------------------------------------
// Sobre (envelope) de 64 bytes
// ---------------------------------------------------------------------------

/// | offset | tamaño | campo       | offset | tamaño | campo       |
/// |--------|--------|-------------|--------|--------|-------------|
/// | 0      | 3      | magic "AGM" | 24     | 8      | source_id   |
/// | 3      | 1      | version = 1 | 32     | 8      | seq         |
/// | 4      | 2      | header_len  | 40     | 8      | t_data (ns) |
/// | 6      | 1      | encoding    | 48     | 8      | t_pub (ns)  |
/// | 7      | 1      | flags       | 56     | 4      | payload_len |
/// | 8      | 8      | schema_id   | 60     | 1      | clock       |
/// | 16     | 8      | topic_id    | 61     | 3      | reservado=0 |
struct Envelope {
    std::uint16_t header_len = kEnvelopeLen;
    Encoding encoding = Encoding::Compact;
    std::uint8_t flags = 0;
    std::uint64_t schema_id = 0;
    std::uint64_t topic_id = 0;
    /// Identificador aleatorio de la instancia que publica.
    std::uint64_t source_id = 0;
    /// Número de secuencia por (source_id, topic_id); empieza en 0.
    std::uint64_t seq = 0;
    /// Instante al que corresponden los datos, en ns desde la época Unix.
    std::int64_t t_data = 0;
    /// Instante en que se publicó, en ns desde la época Unix.
    std::int64_t t_pub = 0;
    std::uint32_t payload_len = 0;
    Clock clock = Clock::Real;

    void write_to(Bytes& out) const;

    /// Lee y valida el sobre (RFC 0001 §8, reglas 1–4). Lanza WireError si
    /// algo no cuadra. Devuelve el sobre y la vista del payload.
    [[nodiscard]] static std::pair<Envelope, ByteView> read_from(ByteView buf);

    friend bool operator==(const Envelope&, const Envelope&) = default;
};

/// Nombre de Agmen ≤ 0.3, por compatibilidad.
using Header = Envelope;

// ---------------------------------------------------------------------------
// Mensajes
// ---------------------------------------------------------------------------

/// Requisitos de un tipo de mensaje. En el nivel 4 estos tipos se van a
/// generar automáticamente desde un archivo de esquema; por ahora se escriben
/// a mano para entender qué hace el generador.
template <class M>
concept Message = requires(const M& msg, Bytes& out, ByteView payload) {
    { M::kName } -> std::convertible_to<std::string_view>;
    { M::kSchema } -> std::convertible_to<std::string_view>;
    msg.encode_payload(out);
    { M::decode_payload(payload) } -> std::same_as<M>;
};

/// Resultado de revisar el esquema de M (en compilación).
template <Message M>
inline constexpr schema::Check schema_check_v = schema::check(M::kSchema);

/// Tamaño del payload compact de M, calculado de su esquema (0 si es arrow).
template <Message M>
inline constexpr std::size_t compact_size_v = schema_check_v<M>.size;

/// ¿M tiene campos de tamaño variable? Entonces viaja en arrow (RFC 0008).
template <Message M>
inline constexpr bool is_arrow_v = schema_check_v<M>.variable;

/// Codificación de M en el sobre.
template <Message M>
inline constexpr Encoding encoding_v = is_arrow_v<M> ? Encoding::Arrow : Encoding::Compact;

/// Máximo de un mensaje: 64 MiB, o AGMEN_MAX_MESSAGE_MB (RFC 0008 §3.5).
[[nodiscard]] std::size_t max_message();

/// Texto del error de un mensaje demasiado grande.
[[nodiscard]] std::string too_big_text(std::string_view what, std::size_t bytes);

/// schema_id de M = FNV-1a 64 de "agmen/1 " + kSchema. Cambia si cambia
/// cualquier campo.
template <Message M>
inline constexpr std::uint64_t schema_id_v = fnv1a64(M::kSchema, fnv1a64(kSchemaPrefix));

/// Nombre de Agmen ≤ 0.3, por compatibilidad.
template <Message M>
inline constexpr std::uint64_t type_id_v = schema_id_v<M>;

/// Texto canónico completo: "agmen/1 " + kSchema.
template <Message M>
[[nodiscard]] std::string schema_text() {
    return std::string(kSchemaPrefix) + std::string(M::kSchema);
}

#define AGMEN_CHECK_SCHEMA(M)                                                               \
    static_assert(::agmen::wire::schema_check_v<M>.ok,                                     \
                  "kSchema no es un esquema canónico de Agmen. Ejemplo válido: "            \
                  "\"MotorCommand{f32 left;f32 right}\" (tipos bool i8 u8 i16 u16 i32 u32 " \
                  "i64 u64 f32 f64 time duration string bytes tensor<T,N>, arreglos "     \
                  "T[N] y T[], structs anidados "                                          \
                  "Nombre{...}; un espacio entre tipo y nombre; ';' entre campos, sin "     \
                  "';' final)")

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
static_assert(compact_size_v<ImuSample> == ImuSample::kPayloadLen);

// ---------------------------------------------------------------------------
// Funciones de alto nivel
// ---------------------------------------------------------------------------

/// Texto de error cuando el payload de un tipo no mide lo que dice su
/// esquema (definido en wire.cpp).
[[nodiscard]] std::string payload_size_text(std::string_view name, std::size_t expected,
                                            std::size_t got);

/// Serializa sobre + payload. De `env` se usan topic_id, source_id, seq,
/// t_data, t_pub, flags y clock; el resto lo llena Agmen.
template <Message M>
[[nodiscard]] Bytes encode(const M& msg, Envelope env) {
    AGMEN_CHECK_SCHEMA(M);
    Bytes out;
    out.reserve(kEnvelopeLen + compact_size_v<M>);
    out.resize(kEnvelopeLen);
    msg.encode_payload(out);
    const std::size_t payload = out.size() - kEnvelopeLen;
    if constexpr (is_arrow_v<M>) {
        if (payload > max_message()) throw WireError(WireError::Kind::TooBig, too_big_text(M::kName, payload));
    } else if (payload != compact_size_v<M>) {
        // Un error de programación: el encode_payload no sigue al esquema.
        throw std::logic_error("encode_payload de '" + std::string(M::kName) + "' produjo " +
                               std::to_string(payload) + " bytes, pero su esquema mide " +
                               std::to_string(compact_size_v<M>));
    }
    env.header_len = kEnvelopeLen;
    env.encoding = encoding_v<M>;
    env.schema_id = schema_id_v<M>;
    env.payload_len = static_cast<std::uint32_t>(payload);
    Bytes header;
    header.reserve(kEnvelopeLen);
    env.write_to(header);
    std::copy(header.begin(), header.end(), out.begin());
    return out;
}

/// Atajo: solo seq y una marca de tiempo (t_data = t_pub = t_ns).
template <Message M>
[[nodiscard]] Bytes encode(const M& msg, std::uint64_t seq, std::int64_t t_ns) {
    return encode(msg, Envelope{.seq = seq, .t_data = t_ns, .t_pub = t_ns});
}

/// Mensaje decodificado junto con su sobre.
template <Message M>
struct Decoded {
    Envelope envelope;
    M message;
};

/// Arma el texto del error de tipo incompatible (definido en wire.cpp).
[[nodiscard]] std::string type_mismatch_text(std::string_view expected_text, std::uint64_t expected,
                                             std::uint64_t got, std::string_view got_detail = {});

/// Decodifica un paquete validando que sea del tipo M. Lanza WireError.
template <Message M>
[[nodiscard]] Decoded<M> decode(ByteView buf) {
    AGMEN_CHECK_SCHEMA(M);
    auto [env, payload] = Envelope::read_from(buf);
    if (env.encoding == Encoding::Announcement) {
        throw WireError(WireError::Kind::NotData,
                        "es un anuncio de esquema, no datos (agmen::Subscriber los maneja solo)");
    }
    if (env.encoding != encoding_v<M>) {
        throw WireError(WireError::Kind::UnknownEncoding,
                        std::string("se esperaba un mensaje ") + (is_arrow_v<M> ? "arrow" : "compact") +
                            " y llegó encoding=" + std::to_string(static_cast<int>(env.encoding)));
    }
    if (env.schema_id != schema_id_v<M>) {
        throw WireError(WireError::Kind::TypeMismatch,
                        type_mismatch_text(schema_text<M>(), schema_id_v<M>, env.schema_id));
    }
    if constexpr (!is_arrow_v<M>) {
        if (payload.size() != compact_size_v<M>) {
            throw WireError(WireError::Kind::BadPayloadSize,
                            payload_size_text(M::kName, compact_size_v<M>, payload.size()));
        }
    }
    return {env, M::decode_payload(payload)};
}

// ---------------------------------------------------------------------------
// Anuncio de esquema (RFC 0001, anexo A)
// ---------------------------------------------------------------------------

/// Lo que dice un anuncio: qué topic y qué esquema usa un emisor.
struct Announcement {
    std::string topic;
    std::string schema;  ///< texto canónico completo, "agmen/1 ..."
};

/// Arma un anuncio. `schema_text` es el texto canónico completo. De `env` se
/// usan source_id, seq, t_pub (t_data = t_pub) y clock.
[[nodiscard]] Bytes encode_announcement(std::string_view schema_text, std::string_view topic,
                                        Envelope env);

/// Valida un anuncio ya leído con Envelope::read_from. Lanza WireError.
[[nodiscard]] Announcement read_announcement(const Envelope& env, ByteView payload);

// ---------------------------------------------------------------------------
// Tiempo e identidad
// ---------------------------------------------------------------------------

/// Tiempo actual en ns desde la época Unix (reloj real de Agmen).
///
/// Sirve para medir latencia entre procesos de la MISMA máquina. Entre
/// máquinas los relojes no están sincronizados (se resuelve en el nivel 11).
/// La hora de Agmen en ns (RFC 0006): en el reloj real, desde la época Unix;
/// en simulación, el tiempo de la simulación. Es la que va en t_data y t_pub.
[[nodiscard]] std::int64_t now_ns();

/// La hora de la computadora (siempre la real). Casi nunca la necesitas.
[[nodiscard]] std::int64_t wall_ns();

/// Identificador aleatorio de 64 bits (≠ 0) para el source_id de una instancia.
[[nodiscard]] std::uint64_t random_source_id();

}  // namespace agmen::wire
