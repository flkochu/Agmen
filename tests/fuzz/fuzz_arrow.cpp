// Fuzzer de la codificación arrow (src/arrow.cpp, RFC 0008).
//
// Entrada: "<esquema>\n<payload arrow>". El esquema también es del fuzzer
// porque las herramientas decodifican con el esquema que se anunció por la
// red. Se vale aceptar o lanzar wire::WireError (payload) o
// schema::SchemaError (esquema). Si el payload se acepta:
//   - cada valor tiene exactamente la forma del esquema (un tensor trae
//     tantos datos como dice su forma, un T[N] de números N·tamaño bytes...);
//   - todos los bytes que apuntan los valores (textos, bytes, números,
//     tensores) están dentro del payload (se leen todos: ASan lo revisa);
//   - cada fila se vuelve a codificar sin error, y lo codificado se decodifica
//     y se vuelve a codificar con exactamente los mismos bytes.
// Cómo correrlo: tests/fuzz/README.md.
#include <agmen/arrow.hpp>
#include <agmen/layout.hpp>
#include <agmen/wire.hpp>

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <string_view>
#include <vector>

namespace {

#define REQUIRE(cond)                                                               \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::fprintf(stderr, "fuzz_arrow: no se cumple %s (%s:%d)\n", #cond, __FILE__, __LINE__); \
            std::abort();                                                           \
        }                                                                           \
    } while (0)

using agmen::arrow::Value;
using agmen::schema::Prim;
using Kind = agmen::schema::Type::Kind;

bool is_number(Prim p) { return p != Prim::Bool; }

Value::Kind scalar_kind(Prim p) {
    switch (p) {
        case Prim::Bool: return Value::Kind::Bool;
        case Prim::U8:
        case Prim::U16:
        case Prim::U32:
        case Prim::U64: return Value::Kind::UInt;
        case Prim::F32:
        case Prim::F64: return Value::Kind::Float;
        default: return Value::Kind::Int;
    }
}

// a·b sin desbordes (o nullopt): para revisar formas con la cuenta exacta.
std::optional<std::uint64_t> mul(std::uint64_t a, std::uint64_t b) {
    if (a != 0 && b > UINT64_MAX / a) return std::nullopt;
    return a * b;
}

// El valor decodificado es exactamente del tipo del esquema: lo que suponen
// el código que genera `agmen gen` y las herramientas al usarlo.
void conforms(const agmen::schema::Layout& l, std::uint32_t ti, const Value& v) {
    const auto& t = l.type(ti);
    switch (t.kind) {
        case Kind::Prim: REQUIRE(v.kind == scalar_kind(t.prim)); return;
        case Kind::String:
        case Kind::Bytes: REQUIRE(v.kind == Value::Kind::Blob); return;
        case Kind::Tensor: {
            REQUIRE(v.kind == Value::Kind::Tensor && v.shape.size() == t.count);
            // Como NumPy: las dimensiones (un 0 cuenta como 1) por el tamaño caben en 63 bits.
            std::optional<std::uint64_t> nominal = agmen::schema::prim_size(t.prim);
            bool zero = false;
            for (const auto d : v.shape) {
                REQUIRE(d >= 0);
                zero = zero || d == 0;
                if (nominal && d != 0) nominal = mul(*nominal, static_cast<std::uint64_t>(d));
            }
            REQUIRE(nominal.has_value() && *nominal <= static_cast<std::uint64_t>(INT64_MAX));
            REQUIRE((zero ? 0 : *nominal) == v.bytes().size());
            return;
        }
        case Kind::List: {
            const auto& e = l.type(t.elem);
            if (e.kind == Kind::Prim && is_number(e.prim)) {
                REQUIRE(v.kind == Value::Kind::Raw && v.bytes().size() % agmen::schema::prim_size(e.prim) == 0);
                return;
            }
            REQUIRE(v.kind == Value::Kind::List);
            for (const auto& x : v.items) conforms(l, t.elem, x);
            return;
        }
        case Kind::Array: {
            std::uint32_t base = ti;
            std::optional<std::uint64_t> n = 1;
            while (l.type(base).kind == Kind::Array) {
                if (n) n = mul(*n, l.type(base).count);
                base = l.type(base).elem;
            }
            const auto& b = l.type(base);
            if (b.kind == Kind::Prim && is_number(b.prim)) {  // números: una tira de prod(dims)
                REQUIRE(n.has_value());
                REQUIRE(v.kind == Value::Kind::Raw && mul(*n, agmen::schema::prim_size(b.prim)) == v.bytes().size());
                return;
            }
            REQUIRE(v.kind == Value::Kind::List && v.items.size() == t.count);
            for (const auto& x : v.items) conforms(l, t.elem, x);
            return;
        }
        case Kind::Struct:
            REQUIRE(v.kind == Value::Kind::Struct && v.items.size() == t.fields.size());
            for (std::size_t k = 0; k < t.fields.size(); ++k) conforms(l, t.fields[k].type, v.items[k]);
            return;
    }
}

// Lee cada byte al que apunta el valor (para que ASan vea cualquier vista rota).
std::uint64_t touch(const Value& v) {
    std::uint64_t sum = 0;
    for (const std::uint8_t b : v.bytes()) sum += b;
    for (const auto& x : v.items) sum += touch(x);
    for (const auto d : v.shape) sum += static_cast<std::uint64_t>(d);
    return sum;
}

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    namespace wire = agmen::wire;
    const std::string_view all(reinterpret_cast<const char*>(data), size);
    const auto nl = all.find('\n');
    if (nl == std::string_view::npos) return 0;

    std::optional<agmen::schema::Layout> l;
    try {
        l.emplace(all.substr(0, nl));
    } catch (const agmen::schema::SchemaError&) {
        return 0;
    }
    if (l->fixed()) return 0;  // compact: no es arrow
    const agmen::arrow::Codec codec(*l);

    const wire::ByteView payload(data + nl + 1, size - nl - 1);
    std::vector<Value> rows;
    try {
        rows = codec.decode_rows(payload);
    } catch (const wire::WireError&) {
        return 0;
    }
    volatile std::uint64_t sink = 0;
    for (const auto& row : rows) {
        conforms(*l, l->root(), row);
        sink = sink + touch(row);
        const wire::Bytes once = codec.encode(row);
        const Value back = codec.decode(once);
        sink = sink + touch(back);
        REQUIRE(codec.encode(back) == once);
    }
    return 0;
}
