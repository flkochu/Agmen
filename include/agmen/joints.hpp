// agmen/joints.hpp — estado y comando de N articulaciones (RFC 0013 §3.4, D5 = f, D7 = a).
//
// El número de articulaciones depende del robot (2 ruedas, 12 servos, 29
// motores) y es parte del tipo: JointState<12> es "JointState con arreglos de
// 12". Son mensajes de Agmen como cualquier tipo generado (compact, por UDP):
//
//     #include <agmen/joints.hpp>
//
//     agmen::Publisher<agmen::msgs::JointState<12>> pub("/patas/estado");
//
//     template <std::size_t N>                     // código general: sirve para 2, 12 o 29
//     void frenar(agmen::msgs::JointCommand<N>& cmd);
//
// En la red no hay nada nuevo: el texto canónico de JointState<12> es
// "JointState{f32[12] position;f32[12] velocity;f32[12] effort}", el mismo que
// da un .agmen con `message JointState { f32[12] position; ... }`. Con N
// distinto el schema_id es otro, así que el hub no deja mezclar 10 y 12 en un
// topic (regla 4). En Python: agmen.msgs.JointState[12] y JointCommand[12],
// con el mismo texto, el mismo schema_id y los mismos bytes
// (tests/vectors/joints.txt).
//
// Unidades (RFC 0013 §3.2): articulaciones giratorias en rad, rad/s y N·m;
// lineales en m, m/s y N. El cero y el sentido positivo son los del modelo del
// robot; una rueda no se "envuelve" (su posición crece sin límite). Lo que no
// se mide o no se manda va en NaN: nunca se inventa un valor (regla 1).
//
// Sin operator==, a propósito: NaN nunca es igual a NaN, así que un mensaje
// recién creado (todo NaN) no sería igual ni a sí mismo. Para comparar, compara
// los bytes (encode_payload) o revisa cada número con std::isnan.
//
// El nombre y el orden de las articulaciones no viajan en el mensaje: van una
// vez, en robot.toml ([[joints]] names, RFC 0013 D6).
#pragma once

#include <agmen/wire.hpp>

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace agmen::msgs {

namespace joints_detail {

/// Lo que cabe en el payload de un mensaje compact: un datagrama UDP (IPv4,
/// 65507 bytes) menos el sobre de 64 (el mismo límite que pone `agmen gen`).
inline constexpr std::size_t kMaxPayload = 65507 - wire::kEnvelopeLen;
/// JointState mide 12·N bytes y JointCommand 20·N: el N más grande que cabe.
inline constexpr std::size_t kMaxStateJoints = kMaxPayload / 12;
inline constexpr std::size_t kMaxCommandJoints = kMaxPayload / 20;
// Los textos de los static_assert de abajo dicen estos números: que no se desfasen.
static_assert(kMaxPayload == 65443 && kMaxStateJoints == 5453 && kMaxCommandJoints == 3272);
static_assert(kMaxStateJoints <= wire::schema::kMaxArray && kMaxCommandJoints <= wire::schema::kMaxArray);

/// El NaN de Agmen para f32: un NaN quieto, 0x7FC00000 (el mismo que pone el
/// código que genera `agmen gen` para `= nan`, y el mismo de Python).
inline constexpr float kNaN = std::bit_cast<float>(std::uint32_t{0x7FC00000U});

// Los nombres locales y de parámetros llevan '_' al inicio, como en el código que genera
// `agmen gen`: así no tapan una variable global del programa que incluye el header (-Wshadow).

/// Cuántas cifras tiene n en decimal (n = 0 tiene una).
constexpr std::size_t digits(std::size_t _n) noexcept {
    std::size_t _d = 1;
    while (_n >= 10) {
        _n /= 10;
        ++_d;
    }
    return _d;
}

/// Largo de "Nombre{f32[N] a;f32[N] b;...}".
template <std::size_t K>
constexpr std::size_t text_size(std::string_view _name, const std::array<std::string_view, K>& _fields,
                                std::size_t _n) noexcept {
    std::size_t _len = _name.size() + 2;  // las llaves
    for (std::string_view _f : _fields) _len += 4 + digits(_n) + 2 + _f.size();  // "f32[" N "] " nombre
    return _len + (K - 1);  // un ';' entre campos, sin ';' final
}

/// Un texto de largo fijo armado al compilar (vive en un `inline constexpr`,
/// así un std::string_view puede apuntar a él).
template <std::size_t L>
struct Text {
    char chars[L];

    [[nodiscard]] constexpr std::string_view view() const noexcept { return {chars, L}; }
};

/// Arma el texto canónico "Nombre{f32[N] a;f32[N] b;...}", con N en decimal y
/// sin ceros a la izquierda. L debe ser text_size(name, fields, n).
template <std::size_t L, std::size_t K>
constexpr Text<L> make_text(std::string_view _name, const std::array<std::string_view, K>& _fields,
                            std::size_t _n) noexcept {
    Text<L> _t{};
    std::size_t _i = 0;
    const auto _put = [&](std::string_view _s) {
        for (char _c : _s) _t.chars[_i++] = _c;
    };
    _put(_name);
    _put("{");
    for (std::size_t _k = 0; _k < K; ++_k) {
        if (_k > 0) _put(";");
        _put("f32[");
        for (std::size_t _p = digits(_n); _p-- > 0;) {  // de la cifra más alta a la más baja
            std::size_t _d = _n;
            for (std::size_t _q = 0; _q < _p; ++_q) _d /= 10;
            _t.chars[_i++] = static_cast<char>('0' + static_cast<int>(_d % 10));
        }
        _put("] ");
        _put(_fields[_k]);
    }
    _put("}");
    return _t;
}

inline constexpr std::string_view kStateName = "JointState";
inline constexpr std::array<std::string_view, 3> kStateFields{"position", "velocity", "effort"};
inline constexpr std::string_view kCommandName = "JointCommand";
inline constexpr std::array<std::string_view, 5> kCommandFields{"position", "velocity", "effort", "kp", "kd"};

template <std::size_t N>
inline constexpr auto kStateText =
    make_text<text_size(kStateName, kStateFields, N)>(kStateName, kStateFields, N);

template <std::size_t N>
inline constexpr auto kCommandText =
    make_text<text_size(kCommandName, kCommandFields, N)>(kCommandName, kCommandFields, N);

template <std::size_t N>
void put_all(wire::Bytes& _out, const std::array<float, N>& _a) {
    for (float _v : _a) wire::put_f32(_out, _v);
}

template <std::size_t N>
void get_all(wire::ByteView _p, std::size_t _at, std::array<float, N>& _a) {
    for (std::size_t _i = 0; _i < N; ++_i) _a[_i] = wire::get_f32(_p, _at + 4 * _i);
}

}  // namespace joints_detail

/// Estado de N articulaciones: lo que se mide. 12·N bytes (144 con 12).
/// Lo que no se mide se queda en NaN (un servo de hobby que no da su posición,
/// un motor sin sensor de par): nunca se devuelve el comando como si fuera una
/// lectura.
/// agmen/1 JointState{f32[N] position;f32[N] velocity;f32[N] effort}
template <std::size_t N>
struct JointState {
    static_assert(N >= 1, "JointState<N>: N es cuántas articulaciones y va de 1 en adelante");
    static_assert(N <= wire::schema::kMaxArray,
                  "JointState<N>: el formato admite arreglos de hasta 1048576 elementos (RFC 0001)");
    static_assert(N <= joints_detail::kMaxStateJoints,
                  "JointState<N> no cabe en un paquete UDP: mide 12·N bytes y caben 65443, así que N va de 1 "
                  "a 5453. Con más articulaciones, repártelas en varios grupos (un topic por grupo)");

    static constexpr std::string_view kName = joints_detail::kStateName;
    static constexpr std::string_view kSchema = joints_detail::kStateText<N>.view();
    static constexpr std::size_t kPayloadLen = 12 * N;

    // Las mismas revisiones que lleva un tipo generado: el texto es canónico,
    // va en compact y su tamaño es el que dice el esquema.
    static_assert(wire::schema::check(kSchema).ok, "JointState<N>: kSchema no es un esquema canónico");
    static_assert(!wire::schema::check(kSchema).variable, "JointState<N> va en compact (tamaño fijo)");
    static_assert(wire::schema::check(kSchema).size == kPayloadLen, "JointState<N>: el tamaño no es 12·N");

    /// Posición: rad (o m en una lineal). NaN = no se mide.
    std::array<float, N> position = wire::filled<float, N>(joints_detail::kNaN);
    /// Velocidad: rad/s (o m/s). NaN = no se mide.
    std::array<float, N> velocity = wire::filled<float, N>(joints_detail::kNaN);
    /// Esfuerzo: par en N·m (o fuerza en N). NaN = no se mide.
    std::array<float, N> effort = wire::filled<float, N>(joints_detail::kNaN);

    /// Escribe los bytes (layout compact) al final de `_out`.
    void encode_into(wire::Bytes& _out) const {
        joints_detail::put_all(_out, position);
        joints_detail::put_all(_out, velocity);
        joints_detail::put_all(_out, effort);
    }

    /// Lee un JointState que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static JointState decode_at(wire::ByteView _p, std::size_t _at) {
        JointState _m;
        joints_detail::get_all(_p, _at, _m.position);
        joints_detail::get_all(_p, _at + 4 * N, _m.velocity);
        joints_detail::get_all(_p, _at + 8 * N, _m.effort);
        return _m;
    }

    void encode_payload(wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static JointState decode_payload(wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                                  wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    // Sin operator==, a propósito (RFC 0013, D5): NaN nunca es igual a NaN.
};

/// Comando de N articulaciones: el de cinco números de Unitree y del
/// mini-cheetah (RFC 0013 D7). 20·N bytes (240 con 12). Quien lo recibe (el
/// driver del motor o el simulador) aplica, por articulación:
///
///     par = kp · (position − q) + kd · (velocity − dq) + effort
///
/// con q y dq la posición y la velocidad medidas. Si `position` es NaN, el
/// primer término no se aplica (control de velocidad o de par). Si `kp` o `kd`
/// son NaN, se usan las ganancias declaradas para el grupo ([[joints]] en
/// robot.toml): las mismas en simulación y en el robot. Con los valores por
/// defecto, quien llena solo `position` obtiene un control de posición con
/// esas ganancias.
///
/// Los ceros no son un valor por defecto: kp = kd = 0 es par puro (solo
/// `effort`), y un robot de patas se cae. Si todo el comando de una
/// articulación es NaN, el driver hace lo mismo que cuando dejan de llegar
/// comandos: su propio freno.
/// agmen/1 JointCommand{f32[N] position;f32[N] velocity;f32[N] effort;f32[N] kp;f32[N] kd}
template <std::size_t N>
struct JointCommand {
    static_assert(N >= 1, "JointCommand<N>: N es cuántas articulaciones y va de 1 en adelante");
    static_assert(N <= wire::schema::kMaxArray,
                  "JointCommand<N>: el formato admite arreglos de hasta 1048576 elementos (RFC 0001)");
    static_assert(N <= joints_detail::kMaxCommandJoints,
                  "JointCommand<N> no cabe en un paquete UDP: mide 20·N bytes y caben 65443, así que N va de 1 "
                  "a 3272. Con más articulaciones, repártelas en varios grupos (un topic por grupo)");

    static constexpr std::string_view kName = joints_detail::kCommandName;
    static constexpr std::string_view kSchema = joints_detail::kCommandText<N>.view();
    static constexpr std::size_t kPayloadLen = 20 * N;

    static_assert(wire::schema::check(kSchema).ok, "JointCommand<N>: kSchema no es un esquema canónico");
    static_assert(!wire::schema::check(kSchema).variable, "JointCommand<N> va en compact (tamaño fijo)");
    static_assert(wire::schema::check(kSchema).size == kPayloadLen, "JointCommand<N>: el tamaño no es 20·N");

    /// Objetivo de posición: rad (o m). NaN = sin objetivo de posición.
    std::array<float, N> position = wire::filled<float, N>(joints_detail::kNaN);
    /// Objetivo de velocidad: rad/s (o m/s).
    std::array<float, N> velocity = wire::filled<float, N>(0.0f);
    /// Par (o fuerza) que se suma directo: N·m (o N).
    std::array<float, N> effort = wire::filled<float, N>(0.0f);
    /// Ganancia de posición: N·m/rad (o N/m). NaN = la declarada para el grupo.
    std::array<float, N> kp = wire::filled<float, N>(joints_detail::kNaN);
    /// Ganancia de velocidad: N·m·s/rad (o N·s/m). NaN = la declarada para el grupo.
    std::array<float, N> kd = wire::filled<float, N>(joints_detail::kNaN);

    /// Escribe los bytes (layout compact) al final de `_out`.
    void encode_into(wire::Bytes& _out) const {
        joints_detail::put_all(_out, position);
        joints_detail::put_all(_out, velocity);
        joints_detail::put_all(_out, effort);
        joints_detail::put_all(_out, kp);
        joints_detail::put_all(_out, kd);
    }

    /// Lee un JointCommand que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static JointCommand decode_at(wire::ByteView _p, std::size_t _at) {
        JointCommand _m;
        joints_detail::get_all(_p, _at, _m.position);
        joints_detail::get_all(_p, _at + 4 * N, _m.velocity);
        joints_detail::get_all(_p, _at + 8 * N, _m.effort);
        joints_detail::get_all(_p, _at + 12 * N, _m.kp);
        joints_detail::get_all(_p, _at + 16 * N, _m.kd);
        return _m;
    }

    void encode_payload(wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static JointCommand decode_payload(wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw wire::WireError(wire::WireError::Kind::BadPayloadSize,
                                  wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    // Sin operator==, a propósito (RFC 0013, D5): NaN nunca es igual a NaN.
};

// Las revisiones de un tipo generado, con dos tamaños de ejemplo (cada N que
// uses pasa además por las de wire::encode y wire::decode al publicar).
static_assert(wire::Message<JointState<12>> && wire::Message<JointCommand<12>>);
static_assert(wire::compact_size_v<JointState<12>> == 144 && wire::compact_size_v<JointCommand<12>> == 240);
static_assert(JointState<12>::kSchema == "JointState{f32[12] position;f32[12] velocity;f32[12] effort}");
static_assert(JointCommand<2>::kSchema ==
              "JointCommand{f32[2] position;f32[2] velocity;f32[2] effort;f32[2] kp;f32[2] kd}");

}  // namespace agmen::msgs
