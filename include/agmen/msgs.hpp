// msgs.agmen
// Generado por `agmen gen` (Agmen 0.14.0) desde msgs.agmen.
// No lo edites: cambia msgs.agmen y vuelve a generar.
#pragma once

#include <agmen/agmen.hpp>

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace agmen::msgs {

/// Una imagen sin comprimir, renglón por renglón. En Python, as_array() la da
/// como numpy.ndarray (alto, ancho, canales) sin copiar, y from_array() la arma
/// desde uno.
/// Imagen · tamaño variable (arrow, por TCP) · Un mensaje: se puede publicar.
/// agmen/1 Imagen{time timestamp;string frame_id;u32 width;u32 height;string encoding;u32 step;bytes data}
struct Imagen {
    static constexpr std::string_view kName = "Imagen";
    static constexpr std::string_view kSchema = "Imagen{time timestamp;string frame_id;u32 width;u32 height;string encoding;u32 step;bytes data}";
    static constexpr std::size_t kPayloadLen = 0;  // arrow: no es fijo

    std::int64_t timestamp{};  ///< cuándo se capturó
    std::string frame_id{};  ///< el marco de referencia (ej. "camara")
    std::uint32_t width{};  ///< ancho en píxeles
    std::uint32_t height{};  ///< alto en píxeles
    std::string encoding = "rgb8";  ///< rgb8, bgr8, rgba8, bgra8, mono8, mono16, 16UC1, 32FC1, bayer_rggb8...
    std::uint32_t step{};  ///< bytes por renglón (0 = ancho × bytes por píxel)
    std::vector<std::uint8_t> data{};  ///< los píxeles

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::integer(timestamp),
            agmen::arrow::Value::text(frame_id),
            agmen::arrow::Value::uinteger(width),
            agmen::arrow::Value::uinteger(height),
            agmen::arrow::Value::text(encoding),
            agmen::arrow::Value::uinteger(step),
            agmen::arrow::Value::blob(agmen::wire::ByteView(data)),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Imagen.
    [[nodiscard]] static Imagen from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 7) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Imagen' debía traer 7 campos");
        }
        Imagen _m;
        _m.timestamp = _v.items[0].as<std::int64_t>();
        _m.frame_id = std::string(_v.items[1].text());
        _m.width = _v.items[2].as<std::uint32_t>();
        _m.height = _v.items[3].as<std::uint32_t>();
        _m.encoding = std::string(_v.items[4].text());
        _m.step = _v.items[5].as<std::uint32_t>();
        _m.data.assign(_v.items[6].bytes().begin(), _v.items[6].bytes().end());
        return _m;
    }

    /// El codificador arrow de este tipo (se arma una vez).
    static const agmen::arrow::Codec& codec() {
        static const agmen::schema::Layout _layout(kSchema);
        static const agmen::arrow::Codec _codec(_layout);
        return _codec;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { codec().encode(to_value(), _out); }

    [[nodiscard]] static Imagen decode_payload(agmen::wire::ByteView _p) {
        return from_value(codec().decode(_p));
    }

    friend bool operator==(const Imagen&, const Imagen&) = default;
};

static_assert(agmen::wire::Message<Imagen>);
static_assert(agmen::wire::is_arrow_v<Imagen>);
static_assert(agmen::wire::schema_id_v<Imagen> == 0x693be29050ec5d1bULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Una imagen comprimida (JPEG, PNG o WebP): el navegador y OpenCV la decodifican.
/// ImagenComprimida · tamaño variable (arrow, por TCP) · Un mensaje: se puede publicar.
/// agmen/1 ImagenComprimida{time timestamp;string frame_id;string format;bytes data}
struct ImagenComprimida {
    static constexpr std::string_view kName = "ImagenComprimida";
    static constexpr std::string_view kSchema = "ImagenComprimida{time timestamp;string frame_id;string format;bytes data}";
    static constexpr std::size_t kPayloadLen = 0;  // arrow: no es fijo

    std::int64_t timestamp{};  ///< cuándo se capturó
    std::string frame_id{};  ///< el marco de referencia (ej. "camara")
    std::string format = "jpeg";  ///< jpeg, png, webp
    std::vector<std::uint8_t> data{};  ///< el archivo comprimido, tal cual

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::integer(timestamp),
            agmen::arrow::Value::text(frame_id),
            agmen::arrow::Value::text(format),
            agmen::arrow::Value::blob(agmen::wire::ByteView(data)),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a ImagenComprimida.
    [[nodiscard]] static ImagenComprimida from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 4) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'ImagenComprimida' debía traer 4 campos");
        }
        ImagenComprimida _m;
        _m.timestamp = _v.items[0].as<std::int64_t>();
        _m.frame_id = std::string(_v.items[1].text());
        _m.format = std::string(_v.items[2].text());
        _m.data.assign(_v.items[3].bytes().begin(), _v.items[3].bytes().end());
        return _m;
    }

    /// El codificador arrow de este tipo (se arma una vez).
    static const agmen::arrow::Codec& codec() {
        static const agmen::schema::Layout _layout(kSchema);
        static const agmen::arrow::Codec _codec(_layout);
        return _codec;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { codec().encode(to_value(), _out); }

    [[nodiscard]] static ImagenComprimida decode_payload(agmen::wire::ByteView _p) {
        return from_value(codec().decode(_p));
    }

    friend bool operator==(const ImagenComprimida&, const ImagenComprimida&) = default;
};

static_assert(agmen::wire::Message<ImagenComprimida>);
static_assert(agmen::wire::is_arrow_v<ImagenComprimida>);
static_assert(agmen::wire::schema_id_v<ImagenComprimida> == 0xaa4de10b3019dd92ULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Quién tiene el mando de un [[mux]] (RFC 0012). El mux lo publica en
/// /mux/<nombre> cada vez que cambia y una vez por segundo. Un nodo que cede el
/// mando (como un modo autónomo) lo lee para saber cuándo retomar.
/// MuxEstado · tamaño variable (arrow, por TCP) · Un mensaje: se puede publicar.
/// agmen/1 MuxEstado{string mux;string entrada;string topic;i32 prioridad;time desde;string antes;bool callada;u64 fuente;u32 cambios}
struct MuxEstado {
    static constexpr std::string_view kName = "MuxEstado";
    static constexpr std::string_view kSchema = "MuxEstado{string mux;string entrada;string topic;i32 prioridad;time desde;string antes;bool callada;u64 fuente;u32 cambios}";
    static constexpr std::size_t kPayloadLen = 0;  // arrow: no es fijo

    std::string mux{};  ///< el nombre del [[mux]] (ej. "ruedas")
    std::string entrada{};  ///< la entrada que manda (ej. "teleop"), o "" si nadie manda
    std::string topic{};  ///< el topic de esa entrada (ej. "/cmd/teleop") o su dirección
    std::int32_t prioridad{};  ///< su prioridad (0 si nadie manda)
    std::int64_t desde{};  ///< desde cuándo manda (o desde cuándo nadie manda), con el reloj de Agmen
    std::string antes{};  ///< la que mandaba antes ("" si nadie)
    bool callada{};  ///< el cambio fue porque 'antes' dejó de hablar (pasaron hold_ms sin mensajes)
    std::uint64_t fuente{};  ///< source_id del publicador que manda (0 si nadie manda)
    std::uint32_t cambios{};  ///< cuántas veces cambió el mando desde que arrancó el mux

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::text(mux),
            agmen::arrow::Value::text(entrada),
            agmen::arrow::Value::text(topic),
            agmen::arrow::Value::integer(prioridad),
            agmen::arrow::Value::integer(desde),
            agmen::arrow::Value::text(antes),
            agmen::arrow::Value::boolean(callada),
            agmen::arrow::Value::uinteger(fuente),
            agmen::arrow::Value::uinteger(cambios),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a MuxEstado.
    [[nodiscard]] static MuxEstado from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 9) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'MuxEstado' debía traer 9 campos");
        }
        MuxEstado _m;
        _m.mux = std::string(_v.items[0].text());
        _m.entrada = std::string(_v.items[1].text());
        _m.topic = std::string(_v.items[2].text());
        _m.prioridad = _v.items[3].as<std::int32_t>();
        _m.desde = _v.items[4].as<std::int64_t>();
        _m.antes = std::string(_v.items[5].text());
        _m.callada = _v.items[6].as<bool>();
        _m.fuente = _v.items[7].as<std::uint64_t>();
        _m.cambios = _v.items[8].as<std::uint32_t>();
        return _m;
    }

    /// El codificador arrow de este tipo (se arma una vez).
    static const agmen::arrow::Codec& codec() {
        static const agmen::schema::Layout _layout(kSchema);
        static const agmen::arrow::Codec _codec(_layout);
        return _codec;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { codec().encode(to_value(), _out); }

    [[nodiscard]] static MuxEstado decode_payload(agmen::wire::ByteView _p) {
        return from_value(codec().decode(_p));
    }

    friend bool operator==(const MuxEstado&, const MuxEstado&) = default;
};

static_assert(agmen::wire::Message<MuxEstado>);
static_assert(agmen::wire::is_arrow_v<MuxEstado>);
static_assert(agmen::wire::schema_id_v<MuxEstado> == 0x3697f7c84d120900ULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Un vector en el plano (los campos de foxglove.Vector2). Las unidades son las
/// del campo que lo usa.
/// Vector2 · 16 bytes · Tipo auxiliar: va dentro de otros.
/// agmen/1 Vector2{f64 x;f64 y}
struct Vector2 {
    static constexpr std::string_view kName = "Vector2";
    static constexpr std::string_view kSchema = "Vector2{f64 x;f64 y}";
    static constexpr std::size_t kPayloadLen = 16;

    double x{};
    double y{};

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        agmen::wire::put_f64(_out, x);
        agmen::wire::put_f64(_out, y);
    }

    /// Lee un Vector2 que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Vector2 decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Vector2 _m;
        _m.x = agmen::wire::get_f64(_p, _at + 0);
        _m.y = agmen::wire::get_f64(_p, _at + 8);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::real(x),
            agmen::arrow::Value::real(y),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Vector2.
    [[nodiscard]] static Vector2 from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 2) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Vector2' debía traer 2 campos");
        }
        Vector2 _m;
        _m.x = _v.items[0].as<double>();
        _m.y = _v.items[1].as<double>();
        return _m;
    }

    friend bool operator==(const Vector2&, const Vector2&) = default;
};

/// Un vector en el espacio (los campos de foxglove.Vector3). Las unidades son
/// las del campo que lo usa (m, m/s, rad/s...).
/// Vector3 · 24 bytes · Tipo auxiliar: va dentro de otros.
/// agmen/1 Vector3{f64 x;f64 y;f64 z}
struct Vector3 {
    static constexpr std::string_view kName = "Vector3";
    static constexpr std::string_view kSchema = "Vector3{f64 x;f64 y;f64 z}";
    static constexpr std::size_t kPayloadLen = 24;

    double x{};
    double y{};
    double z{};

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        agmen::wire::put_f64(_out, x);
        agmen::wire::put_f64(_out, y);
        agmen::wire::put_f64(_out, z);
    }

    /// Lee un Vector3 que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Vector3 decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Vector3 _m;
        _m.x = agmen::wire::get_f64(_p, _at + 0);
        _m.y = agmen::wire::get_f64(_p, _at + 8);
        _m.z = agmen::wire::get_f64(_p, _at + 16);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::real(x),
            agmen::arrow::Value::real(y),
            agmen::arrow::Value::real(z),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Vector3.
    [[nodiscard]] static Vector3 from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 3) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Vector3' debía traer 3 campos");
        }
        Vector3 _m;
        _m.x = _v.items[0].as<double>();
        _m.y = _v.items[1].as<double>();
        _m.z = _v.items[2].as<double>();
        return _m;
    }

    friend bool operator==(const Vector3&, const Vector3&) = default;
};

/// Una rotación (los campos de foxglove.Quaternion): x, y, z, w, con w al final.
/// Empieza en "sin rotación" (w = 1) porque se calcula. En Python, wxyz() y
/// from_wxyz(); en C++, agmen::msgs::wxyz() y from_wxyz() (<agmen/geometry.hpp>).
/// Quaternion · 32 bytes · Tipo auxiliar: va dentro de otros.
/// agmen/1 Quaternion{f64 x;f64 y;f64 z;f64 w}
struct Quaternion {
    static constexpr std::string_view kName = "Quaternion";
    static constexpr std::string_view kSchema = "Quaternion{f64 x;f64 y;f64 z;f64 w}";
    static constexpr std::size_t kPayloadLen = 32;

    double x{};
    double y{};
    double z{};
    double w = 1.0;  ///< w = 1, x = y = z = 0: sin rotación

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        agmen::wire::put_f64(_out, x);
        agmen::wire::put_f64(_out, y);
        agmen::wire::put_f64(_out, z);
        agmen::wire::put_f64(_out, w);
    }

    /// Lee un Quaternion que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Quaternion decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Quaternion _m;
        _m.x = agmen::wire::get_f64(_p, _at + 0);
        _m.y = agmen::wire::get_f64(_p, _at + 8);
        _m.z = agmen::wire::get_f64(_p, _at + 16);
        _m.w = agmen::wire::get_f64(_p, _at + 24);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::real(x),
            agmen::arrow::Value::real(y),
            agmen::arrow::Value::real(z),
            agmen::arrow::Value::real(w),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Quaternion.
    [[nodiscard]] static Quaternion from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 4) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Quaternion' debía traer 4 campos");
        }
        Quaternion _m;
        _m.x = _v.items[0].as<double>();
        _m.y = _v.items[1].as<double>();
        _m.z = _v.items[2].as<double>();
        _m.w = _v.items[3].as<double>();
        return _m;
    }

    friend bool operator==(const Quaternion&, const Quaternion&) = default;
};

/// Una pose (los campos de foxglove.Pose): dónde está y cómo está girado algo,
/// en el marco del topic.
/// Pose · 56 bytes · Un mensaje: se puede publicar.
/// agmen/1 Pose{Vector3{f64 x;f64 y;f64 z} position;Quaternion{f64 x;f64 y;f64 z;f64 w} orientation}
struct Pose {
    static constexpr std::string_view kName = "Pose";
    static constexpr std::string_view kSchema = "Pose{Vector3{f64 x;f64 y;f64 z} position;Quaternion{f64 x;f64 y;f64 z;f64 w} orientation}";
    static constexpr std::size_t kPayloadLen = 56;

    Vector3 position{};  ///< m
    Quaternion orientation{};

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        position.encode_into(_out);
        orientation.encode_into(_out);
    }

    /// Lee un Pose que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Pose decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Pose _m;
        _m.position = Vector3::decode_at(_p, _at + 0);
        _m.orientation = Quaternion::decode_at(_p, _at + 24);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            position.to_value(),
            orientation.to_value(),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Pose.
    [[nodiscard]] static Pose from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 2) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Pose' debía traer 2 campos");
        }
        Pose _m;
        _m.position = Vector3::from_value(_v.items[0]);
        _m.orientation = Quaternion::from_value(_v.items[1]);
        return _m;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static Pose decode_payload(agmen::wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadPayloadSize,
                                         agmen::wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    friend bool operator==(const Pose&, const Pose&) = default;
};

static_assert(agmen::wire::Message<Pose>);
static_assert(agmen::wire::compact_size_v<Pose> == Pose::kPayloadLen);
static_assert(agmen::wire::schema_id_v<Pose> == 0x0ddd001323c61ac4ULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Una transformación entre dos marcos (los campos de foxglove.FrameTransform,
/// sin los nombres): los marcos padre e hijo los dice robot.toml.
/// Transform · 56 bytes · Un mensaje: se puede publicar.
/// agmen/1 Transform{Vector3{f64 x;f64 y;f64 z} translation;Quaternion{f64 x;f64 y;f64 z;f64 w} rotation}
struct Transform {
    static constexpr std::string_view kName = "Transform";
    static constexpr std::string_view kSchema = "Transform{Vector3{f64 x;f64 y;f64 z} translation;Quaternion{f64 x;f64 y;f64 z;f64 w} rotation}";
    static constexpr std::size_t kPayloadLen = 56;

    Vector3 translation{};  ///< dónde está el origen del hijo, visto desde el padre (m)
    Quaternion rotation{};  ///< cómo está girado el hijo, visto desde el padre

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        translation.encode_into(_out);
        rotation.encode_into(_out);
    }

    /// Lee un Transform que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Transform decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Transform _m;
        _m.translation = Vector3::decode_at(_p, _at + 0);
        _m.rotation = Quaternion::decode_at(_p, _at + 24);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            translation.to_value(),
            rotation.to_value(),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Transform.
    [[nodiscard]] static Transform from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 2) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Transform' debía traer 2 campos");
        }
        Transform _m;
        _m.translation = Vector3::from_value(_v.items[0]);
        _m.rotation = Quaternion::from_value(_v.items[1]);
        return _m;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static Transform decode_payload(agmen::wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadPayloadSize,
                                         agmen::wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    friend bool operator==(const Transform&, const Transform&) = default;
};

static_assert(agmen::wire::Message<Transform>);
static_assert(agmen::wire::compact_size_v<Transform> == Transform::kPayloadLen);
static_assert(agmen::wire::schema_id_v<Transform> == 0x6d70abe3cd89424fULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Una velocidad (los campos de geometry_msgs/Twist): para mover un robot, en
/// el marco de su base (base_link).
/// Twist · 48 bytes · Un mensaje: se puede publicar.
/// agmen/1 Twist{Vector3{f64 x;f64 y;f64 z} linear;Vector3{f64 x;f64 y;f64 z} angular}
struct Twist {
    static constexpr std::string_view kName = "Twist";
    static constexpr std::string_view kSchema = "Twist{Vector3{f64 x;f64 y;f64 z} linear;Vector3{f64 x;f64 y;f64 z} angular}";
    static constexpr std::size_t kPayloadLen = 48;

    Vector3 linear{};  ///< m/s, en el marco del topic (para mover un robot: su base_link)
    Vector3 angular{};  ///< rad/s, en el mismo marco

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        linear.encode_into(_out);
        angular.encode_into(_out);
    }

    /// Lee un Twist que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Twist decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Twist _m;
        _m.linear = Vector3::decode_at(_p, _at + 0);
        _m.angular = Vector3::decode_at(_p, _at + 24);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            linear.to_value(),
            angular.to_value(),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Twist.
    [[nodiscard]] static Twist from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 2) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Twist' debía traer 2 campos");
        }
        Twist _m;
        _m.linear = Vector3::from_value(_v.items[0]);
        _m.angular = Vector3::from_value(_v.items[1]);
        return _m;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static Twist decode_payload(agmen::wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadPayloadSize,
                                         agmen::wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    friend bool operator==(const Twist&, const Twist&) = default;
};

static_assert(agmen::wire::Message<Twist>);
static_assert(agmen::wire::compact_size_v<Twist> == Twist::kPayloadLen);
static_assert(agmen::wire::schema_id_v<Twist> == 0xd20d1470e78f60acULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Odometría (los campos de foxglove.Odometry, sin texto ni covarianzas): el
/// hijo (ej. base_link) visto desde el padre (ej. odom); los dos marcos los dice
/// el [[frame]] de robot.toml cuyo topic es este.
/// Odometry · 104 bytes · Un mensaje: se puede publicar.
/// agmen/1 Odometry{Pose{Vector3{f64 x;f64 y;f64 z} position;Quaternion{f64 x;f64 y;f64 z;f64 w} orientation} pose;Vector3{f64 x;f64 y;f64 z} linear_velocity;Vector3{f64 x;f64 y;f64 z} angular_velocity}
struct Odometry {
    static constexpr std::string_view kName = "Odometry";
    static constexpr std::string_view kSchema = "Odometry{Pose{Vector3{f64 x;f64 y;f64 z} position;Quaternion{f64 x;f64 y;f64 z;f64 w} orientation} pose;Vector3{f64 x;f64 y;f64 z} linear_velocity;Vector3{f64 x;f64 y;f64 z} angular_velocity}";
    static constexpr std::size_t kPayloadLen = 104;

    Pose pose{};  ///< el hijo visto desde el padre (ej. base_link en odom)
    Vector3 linear_velocity{};  ///< m/s, medida en el hijo (como body_frame_id en Foxglove)
    Vector3 angular_velocity{};  ///< rad/s, medida en el hijo

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        pose.encode_into(_out);
        linear_velocity.encode_into(_out);
        angular_velocity.encode_into(_out);
    }

    /// Lee un Odometry que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Odometry decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Odometry _m;
        _m.pose = Pose::decode_at(_p, _at + 0);
        _m.linear_velocity = Vector3::decode_at(_p, _at + 56);
        _m.angular_velocity = Vector3::decode_at(_p, _at + 80);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            pose.to_value(),
            linear_velocity.to_value(),
            angular_velocity.to_value(),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Odometry.
    [[nodiscard]] static Odometry from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 3) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Odometry' debía traer 3 campos");
        }
        Odometry _m;
        _m.pose = Pose::from_value(_v.items[0]);
        _m.linear_velocity = Vector3::from_value(_v.items[1]);
        _m.angular_velocity = Vector3::from_value(_v.items[2]);
        return _m;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static Odometry decode_payload(agmen::wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadPayloadSize,
                                         agmen::wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    friend bool operator==(const Odometry&, const Odometry&) = default;
};

static_assert(agmen::wire::Message<Odometry>);
static_assert(agmen::wire::compact_size_v<Odometry> == Odometry::kPayloadLen);
static_assert(agmen::wire::schema_id_v<Odometry> == 0x284dc2a8b388e10fULL,
              "el schema_id de C++ no coincide con el de agmen gen");

/// Un vector en el espacio en f32, para lo que sale de un microcontrolador (la
/// IMU). Las unidades son las del campo que lo usa.
/// Vector3f · 12 bytes · Tipo auxiliar: va dentro de otros.
/// agmen/1 Vector3f{f32 x;f32 y;f32 z}
struct Vector3f {
    static constexpr std::string_view kName = "Vector3f";
    static constexpr std::string_view kSchema = "Vector3f{f32 x;f32 y;f32 z}";
    static constexpr std::size_t kPayloadLen = 12;

    float x{};
    float y{};
    float z{};

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        agmen::wire::put_f32(_out, x);
        agmen::wire::put_f32(_out, y);
        agmen::wire::put_f32(_out, z);
    }

    /// Lee un Vector3f que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Vector3f decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Vector3f _m;
        _m.x = agmen::wire::get_f32(_p, _at + 0);
        _m.y = agmen::wire::get_f32(_p, _at + 4);
        _m.z = agmen::wire::get_f32(_p, _at + 8);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::real(x),
            agmen::arrow::Value::real(y),
            agmen::arrow::Value::real(z),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Vector3f.
    [[nodiscard]] static Vector3f from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 3) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Vector3f' debía traer 3 campos");
        }
        Vector3f _m;
        _m.x = _v.items[0].as<float>();
        _m.y = _v.items[1].as<float>();
        _m.z = _v.items[2].as<float>();
        return _m;
    }

    friend bool operator==(const Vector3f&, const Vector3f&) = default;
};

/// Una rotación en f32: x, y, z, w, con w al final. Empieza en NaN porque se
/// mide: vacía hasta que la llenes (lo que no se mide no se inventa). En Python,
/// wxyz() y from_wxyz(); en C++, agmen::msgs::wxyz() y from_wxyz()
/// (<agmen/geometry.hpp>).
/// Quaternionf · 16 bytes · Tipo auxiliar: va dentro de otros.
/// agmen/1 Quaternionf{f32 x;f32 y;f32 z;f32 w}
struct Quaternionf {
    static constexpr std::string_view kName = "Quaternionf";
    static constexpr std::string_view kSchema = "Quaternionf{f32 x;f32 y;f32 z;f32 w}";
    static constexpr std::size_t kPayloadLen = 16;

    float x = std::bit_cast<float>(0x7FC00000U);
    float y = std::bit_cast<float>(0x7FC00000U);
    float z = std::bit_cast<float>(0x7FC00000U);
    float w = std::bit_cast<float>(0x7FC00000U);

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        agmen::wire::put_f32(_out, x);
        agmen::wire::put_f32(_out, y);
        agmen::wire::put_f32(_out, z);
        agmen::wire::put_f32(_out, w);
    }

    /// Lee un Quaternionf que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Quaternionf decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Quaternionf _m;
        _m.x = agmen::wire::get_f32(_p, _at + 0);
        _m.y = agmen::wire::get_f32(_p, _at + 4);
        _m.z = agmen::wire::get_f32(_p, _at + 8);
        _m.w = agmen::wire::get_f32(_p, _at + 12);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            agmen::arrow::Value::real(x),
            agmen::arrow::Value::real(y),
            agmen::arrow::Value::real(z),
            agmen::arrow::Value::real(w),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Quaternionf.
    [[nodiscard]] static Quaternionf from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 4) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Quaternionf' debía traer 4 campos");
        }
        Quaternionf _m;
        _m.x = _v.items[0].as<float>();
        _m.y = _v.items[1].as<float>();
        _m.z = _v.items[2].as<float>();
        _m.w = _v.items[3].as<float>();
        return _m;
    }

    friend bool operator==(const Quaternionf&, const Quaternionf&) = default;
};

/// Una IMU (los campos de sensor_msgs/Imu, sin covarianzas, en f32). La
/// orientación es respecto a un marco quieto con z hacia arriba (alineado con la
/// gravedad); hacia dónde apunta x es arbitrario (donde quedó al encender),
/// salvo que tenga magnetómetro. ImuSample, la del tutorial, no cambia.
/// Imu · 40 bytes · Un mensaje: se puede publicar.
/// agmen/1 Imu{Quaternionf{f32 x;f32 y;f32 z;f32 w} orientation;Vector3f{f32 x;f32 y;f32 z} angular_velocity;Vector3f{f32 x;f32 y;f32 z} linear_acceleration}
struct Imu {
    static constexpr std::string_view kName = "Imu";
    static constexpr std::string_view kSchema = "Imu{Quaternionf{f32 x;f32 y;f32 z;f32 w} orientation;Vector3f{f32 x;f32 y;f32 z} angular_velocity;Vector3f{f32 x;f32 y;f32 z} linear_acceleration}";
    static constexpr std::size_t kPayloadLen = 40;

    Quaternionf orientation{};  ///< la IMU vista desde un marco quieto con z arriba; NaN si no la estima
    Vector3f angular_velocity{};  ///< rad/s, en el marco de la IMU
    Vector3f linear_acceleration{};  ///< m/s², en el marco de la IMU (quieta y con z arriba marca +9.81 en z)

    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.
    void encode_into(agmen::wire::Bytes& _out) const {
        orientation.encode_into(_out);
        angular_velocity.encode_into(_out);
        linear_acceleration.encode_into(_out);
    }

    /// Lee un Imu que empieza en el byte `_at` (el tamaño ya se revisó).
    [[nodiscard]] static Imu decode_at(agmen::wire::ByteView _p, std::size_t _at) {
        Imu _m;
        _m.orientation = Quaternionf::decode_at(_p, _at + 0);
        _m.angular_velocity = Vector3f::decode_at(_p, _at + 16);
        _m.linear_acceleration = Vector3f::decode_at(_p, _at + 28);
        return _m;
    }

    /// El struct como valor arrow. Presta los textos, bytes y arreglos: úsalo de inmediato.
    [[nodiscard]] agmen::arrow::Value to_value() const {
        return agmen::arrow::Value::structure({
            orientation.to_value(),
            angular_velocity.to_value(),
            linear_acceleration.to_value(),
        });
    }

    /// De un valor arrow (lo que decodifica agmen::arrow::Codec) a Imu.
    [[nodiscard]] static Imu from_value(const agmen::arrow::Value& _v) {
        if (_v.kind != agmen::arrow::Value::Kind::Struct || _v.items.size() != 3) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadArrow,
                                         "'Imu' debía traer 3 campos");
        }
        Imu _m;
        _m.orientation = Quaternionf::from_value(_v.items[0]);
        _m.angular_velocity = Vector3f::from_value(_v.items[1]);
        _m.linear_acceleration = Vector3f::from_value(_v.items[2]);
        return _m;
    }

    void encode_payload(agmen::wire::Bytes& _out) const { encode_into(_out); }

    [[nodiscard]] static Imu decode_payload(agmen::wire::ByteView _p) {
        if (_p.size() != kPayloadLen) {
            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadPayloadSize,
                                         agmen::wire::payload_size_text(kName, kPayloadLen, _p.size()));
        }
        return decode_at(_p, 0);
    }

    friend bool operator==(const Imu&, const Imu&) = default;
};

static_assert(agmen::wire::Message<Imu>);
static_assert(agmen::wire::compact_size_v<Imu> == Imu::kPayloadLen);
static_assert(agmen::wire::schema_id_v<Imu> == 0xb656f8a397bcbc97ULL,
              "el schema_id de C++ no coincide con el de agmen gen");

}  // namespace agmen::msgs
