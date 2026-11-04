// agmen/layout.hpp — esquemas en tiempo de ejecución (RFC 0007, nivel 6).
//
// El mismo trabajo que agmen.schema en Python: lee el texto de un esquema
// ("MotorCommand{f32 left;f32 right}"), da su texto canónico, su schema_id y
// el layout compact (dónde va cada campo). Sirve para manejar tipos que no
// se compilaron: el módulo de Python (agmen._core) y, más adelante, el puente
// del panel en C++.
//
//   const agmen::schema::Layout l("Pose{Vec3{f64 x;f64 y;f64 z} p;f32 yaw}");
//   l.text();        // "agmen/1 Pose{Vec3{f64 x;f64 y;f64 z} p;f32 yaw}"
//   l.size();        // 32
//   l.schema_id();   // 0xdea73a0a8e3d8e97
//
// Desde Agmen 0.10 (RFC 0008) también lee los tipos de tamaño variable:
// string, bytes, T[] y tensor<T,N>. Un esquema con alguno de ellos no es
// `fixed()` y viaja en la codificación arrow (agmen/arrow.hpp).
#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace agmen::schema {

/// El texto del esquema no es válido. El mensaje dice dónde y por qué.
class SchemaError : public std::invalid_argument {
public:
    using std::invalid_argument::invalid_argument;
};

enum class Prim : std::uint8_t { Bool, I8, U8, I16, U16, I32, U32, I64, U64, F32, F64, Time, Duration };

[[nodiscard]] std::size_t prim_size(Prim p);
[[nodiscard]] std::string_view prim_name(Prim p);

/// Un tipo del árbol. Los tipos viven en Layout::types(); se refieren entre
/// sí por índice.
struct Type {
    /// String y Bytes: tamaño variable. List = T[]. Tensor = tensor<T,N>.
    enum class Kind : std::uint8_t { Prim, Array, Struct, String, Bytes, List, Tensor };
    struct Field {
        std::string name;
        std::uint32_t type = 0;
        std::size_t offset = 0;
    };

    Kind kind = Kind::Prim;
    Prim prim = Prim::U8;          ///< Kind::Prim; Kind::Tensor: el tipo de los elementos
    std::uint32_t count = 0;       ///< Kind::Array: número de elementos; Kind::Tensor: dimensiones
    std::uint32_t elem = 0;        ///< Kind::Array y Kind::List: índice del tipo de cada elemento
    std::string name;              ///< Kind::Struct
    std::vector<Field> fields;     ///< Kind::Struct
    bool fixed = true;             ///< tamaño fijo (si no, size y align no aplican)
    std::size_t size = 0;
    std::size_t align = 1;
};

class Layout {
public:
    /// Acepta el texto con o sin "agmen/1 " y con espacios de más. Lanza
    /// SchemaError si no es válido.
    explicit Layout(std::string_view text);

    /// Texto canónico completo: "agmen/1 Nombre{...}".
    [[nodiscard]] const std::string& text() const { return text_; }
    [[nodiscard]] std::uint64_t schema_id() const { return schema_id_; }
    [[nodiscard]] const std::string& name() const { return types_[root_].name; }
    /// Tamaño del payload compact (una fila). 0 si el esquema no es fixed().
    [[nodiscard]] std::size_t size() const { return types_[root_].fixed ? types_[root_].size : 0; }
    /// ¿Todos los campos son de tamaño fijo? Entonces viaja en compact; si no, en arrow.
    [[nodiscard]] bool fixed() const { return types_[root_].fixed; }

    [[nodiscard]] std::uint32_t root() const { return root_; }
    [[nodiscard]] const Type& type(std::uint32_t i) const { return types_[i]; }
    [[nodiscard]] const std::vector<Type>& types() const { return types_; }

    /// Canónico de un tipo del árbol ("f32[3]", "Vec3{f64 x;f64 y;f64 z}").
    [[nodiscard]] std::string canonical(std::uint32_t i) const;

private:
    std::vector<Type> types_;
    std::uint32_t root_ = 0;
    std::string text_;
    std::uint64_t schema_id_ = 0;

    friend struct LayoutParser;
};

}  // namespace agmen::schema
