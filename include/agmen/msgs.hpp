// msgs.agmen
// Generado por `agmen gen` (Agmen 0.11.0) desde msgs.agmen.
// No lo edites: cambia msgs.agmen y vuelve a generar.
#pragma once

#include <agmen/agmen.hpp>

#include <array>
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

}  // namespace agmen::msgs
