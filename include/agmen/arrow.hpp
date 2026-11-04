// agmen/arrow.hpp — la codificación arrow en C++ (RFC 0001 §6, RFC 0008).
//
// Los mensajes con campos de tamaño variable (string, bytes, T[], tensor<T,N>)
// viajan como un mensaje encapsulado de Arrow IPC con un RecordBatch. Es la
// misma implementación que python/agmen/arrow.py (la referencia) y que
// agmen.js: los tres dan los mismos bytes (tests/vectors/v1.txt).
//
// El código generado por `agmen gen` usa esto por dentro; casi nunca lo
// necesitas directamente. Sirve para herramientas que manejan tipos que no se
// compilaron:
//
//   const agmen::schema::Layout l("Texto{string s;u8 n}");
//   const agmen::arrow::Codec codec(l);
//   auto bytes = codec.encode(Value::structure({Value::text("hola"), Value::integer(7)}));
//   auto v = codec.decode(bytes);          // v.items[0].text() == "hola"
//
// Al decodificar, los textos, bytes y arreglos de números NO se copian:
// apuntan al payload, que debe vivir más que el Value.
#pragma once

#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

#include "agmen/layout.hpp"
#include "agmen/wire.hpp"

namespace agmen {

static_assert(std::endian::native == std::endian::little,
              "Agmen supone una computadora little-endian (x86, ARM): el formato también lo es");

/// Un tensor<T,N>: su forma y sus datos en orden de renglones (row-major).
template <class T>
struct Tensor {
    std::vector<std::int32_t> shape;
    std::vector<T> data;

    friend bool operator==(const Tensor&, const Tensor&) = default;
};

}  // namespace agmen

namespace agmen::arrow {

/// Un valor de un mensaje, sin tipo en compilación.
struct Value {
    enum class Kind : std::uint8_t {
        None,
        Bool,
        Int,     ///< iN, time, duration
        UInt,    ///< uN
        Float,   ///< f32, f64
        Blob,    ///< string (UTF-8) o bytes
        Raw,     ///< números seguidos en little-endian (T[] o T[N] de números)
        List,    ///< T[] o T[N] de otra cosa: los elementos en items
        Struct,  ///< los campos en items, en el orden del esquema
        Tensor,  ///< shape + datos (los bytes, como Raw)
    };

    Kind kind = Kind::None;
    bool b = false;
    std::int64_t i = 0;
    std::uint64_t u = 0;
    double f = 0;
    std::vector<Value> items;
    std::vector<std::int32_t> shape;

    /// Los bytes de un Blob, Raw o Tensor (prestados o propios).
    [[nodiscard]] wire::ByteView bytes() const { return store_ ? wire::ByteView(*store_) : view_; }
    [[nodiscard]] std::string_view text() const {
        const auto v = bytes();
        return {reinterpret_cast<const char*>(v.data()), v.size()};
    }

    static Value boolean(bool v) {
        Value x;
        x.kind = Kind::Bool;
        x.b = v;
        return x;
    }
    static Value integer(std::int64_t v) {
        Value x;
        x.kind = Kind::Int;
        x.i = v;
        return x;
    }
    static Value uinteger(std::uint64_t v) {
        Value x;
        x.kind = Kind::UInt;
        x.u = v;
        return x;
    }
    static Value real(double v) {
        Value x;
        x.kind = Kind::Float;
        x.f = v;
        return x;
    }
    /// Texto o bytes prestados: `data` debe vivir mientras se use el Value.
    static Value blob(wire::ByteView data) {
        Value x;
        x.kind = Kind::Blob;
        x.view_ = data;
        return x;
    }
    static Value text(std::string_view s) {
        return blob({reinterpret_cast<const std::uint8_t*>(s.data()), s.size()});
    }
    /// Una copia propia de los bytes.
    static Value blob_copy(wire::ByteView data) {
        Value x;
        x.kind = Kind::Blob;
        x.store_ = std::make_shared<std::vector<std::uint8_t>>(data.begin(), data.end());
        return x;
    }
    /// Números seguidos, prestados (std::vector<float>, std::array<float, N>...).
    template <class T>
    static Value raw(std::span<const T> values) {
        static_assert(std::is_arithmetic_v<T> && !std::is_same_v<T, bool>);
        Value x;
        x.kind = Kind::Raw;
        x.view_ = std::as_bytes(values).size() == 0
                      ? wire::ByteView{}
                      : wire::ByteView(reinterpret_cast<const std::uint8_t*>(values.data()), values.size_bytes());
        return x;
    }
    static Value raw_bytes(wire::ByteView data) {
        Value x;
        x.kind = Kind::Raw;
        x.view_ = data;
        return x;
    }
    static Value list(std::vector<Value> v) {
        Value x;
        x.kind = Kind::List;
        x.items = std::move(v);
        return x;
    }
    static Value structure(std::vector<Value> fields) {
        Value x;
        x.kind = Kind::Struct;
        x.items = std::move(fields);
        return x;
    }
    template <class T>
    static Value tensor(std::vector<std::int32_t> shape, std::span<const T> data) {
        Value x = raw(data);
        x.kind = Kind::Tensor;
        x.shape = std::move(shape);
        return x;
    }

    /// Un número como T (de Int, UInt, Float o Bool).
    template <class T>
    [[nodiscard]] T as() const {
        switch (kind) {
            case Kind::Bool: return static_cast<T>(b);
            case Kind::Int: return static_cast<T>(i);
            case Kind::UInt: return static_cast<T>(u);
            case Kind::Float: return static_cast<T>(f);
            default: return T{};
        }
    }

    /// Los números de un Raw o Tensor, copiados a un vector.
    template <class T>
    [[nodiscard]] std::vector<T> numbers() const {
        const auto v = bytes();
        std::vector<T> out(v.size() / sizeof(T));
        if (!out.empty()) std::memcpy(out.data(), v.data(), out.size() * sizeof(T));
        return out;
    }

private:
    wire::ByteView view_{};
    std::shared_ptr<std::vector<std::uint8_t>> store_;
};

/// El codificador arrow de un esquema (que no sea fixed()).
class Codec {
public:
    explicit Codec(const schema::Layout& layout);

    /// Valores de una fila (Value::structure con un item por campo) → payload,
    /// agregado al final de `out`. Lanza schema::SchemaError si un valor no
    /// sigue al esquema ("'q' debe tener 12 elementos, tiene 11").
    void encode(const Value& row, wire::Bytes& out) const;
    [[nodiscard]] wire::Bytes encode(const Value& row) const;

    /// payload → la fila (Kind::Struct). Lanza wire::WireError si no es Arrow
    /// válido o no coincide con el esquema.
    [[nodiscard]] Value decode(wire::ByteView payload) const;
    /// Un RecordBatch puede traer varias filas (RFC 0001 §6).
    [[nodiscard]] std::vector<Value> decode_rows(wire::ByteView payload) const;

    struct Node {
        enum class Kind : std::uint8_t { Prim, Bool, Utf8, Binary, List, Fsl, Struct, Tensor };
        Kind kind = Kind::Prim;
        schema::Prim prim = schema::Prim::U8;
        std::size_t size = 0;            ///< bytes de un número (Prim, y el hijo de Tensor)
        std::size_t n = 0;               ///< Fsl: elementos por fila (N·M si es T[N][M] de números)
        std::vector<std::uint32_t> dims; ///< Fsl: las dimensiones
        std::uint32_t ndim = 0;          ///< Tensor
        std::vector<std::uint32_t> children;
        std::vector<std::string> names;  ///< Struct
    };

private:
    const schema::Layout* layout_;
    std::vector<Node> nodes_;
    std::uint32_t root_ = 0;

    std::uint32_t build(std::uint32_t type);
    friend struct Writer;
    friend struct Reader;
};

}  // namespace agmen::arrow
