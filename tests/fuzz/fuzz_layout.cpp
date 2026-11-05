// Fuzzer del texto de un esquema (src/layout.cpp y agmen::wire::schema::check).
//
// El texto de un esquema también llega de la red, en los anuncios: las
// herramientas (`agmen echo`, el panel) arman un Layout con lo que llegue. Se
// vale aceptarlo o lanzar schema::SchemaError. Además, los dos lectores de C++
// tienen que estar de acuerdo:
//   - check() (el de compilación) solo acepta texto canónico; si lo acepta,
//     Layout también, con el mismo texto, tamaño y alineación;
//   - el texto canónico que da Layout se vuelve a leer igual y check() lo acepta.
// Cómo correrlo: tests/fuzz/README.md.
#include <agmen/arrow.hpp>
#include <agmen/layout.hpp>
#include <agmen/wire.hpp>

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <string>
#include <string_view>

namespace {

#define REQUIRE(cond)                                                                \
    do {                                                                             \
        if (!(cond)) {                                                               \
            std::fprintf(stderr, "fuzz_layout: no se cumple %s (%s:%d)\n", #cond, __FILE__, __LINE__); \
            std::abort();                                                            \
        }                                                                            \
    } while (0)

void same_as_check(const agmen::schema::Layout& l, const agmen::wire::schema::Check& c) {
    REQUIRE(c.ok);
    REQUIRE(c.variable == !l.fixed());
    if (l.fixed()) {
        REQUIRE(c.size == l.size());
        REQUIRE(c.align == l.type(l.root()).align);
    }
}

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    namespace wire = agmen::wire;
    namespace schema = agmen::schema;
    const std::string_view text(reinterpret_cast<const char*>(data), size);

    const wire::schema::Check c = wire::schema::check(text);
    std::optional<schema::Layout> l;
    try {
        l.emplace(text);
    } catch (const schema::SchemaError&) {
    }
    if (c.ok) {  // canónico (sin el prefijo): Layout lo lee igual
        REQUIRE(l.has_value());
        REQUIRE(l->text() == std::string(wire::kSchemaPrefix) + std::string(text));
        same_as_check(*l, c);
    }
    if (!l) return 0;

    // El canónico se vuelve a leer igual, y check() lo acepta.
    const schema::Layout again(l->text());
    REQUIRE(again.text() == l->text());
    REQUIRE(again.schema_id() == l->schema_id());
    REQUIRE(again.size() == l->size());
    REQUIRE(l->schema_id() == wire::fnv1a64(l->text()));
    same_as_check(*l, wire::schema::check(std::string_view(l->text()).substr(wire::kSchemaPrefix.size())));

    // El tamaño de un tipo fijo es exacto (sin desbordes): un arreglo mide
    // count × su elemento, y los campos de un struct van en orden, alineados y
    // sin pisarse.
    for (const auto& t : l->types()) {
        if (!t.fixed) continue;
        REQUIRE(t.size >= 1);
        REQUIRE(t.align >= 1 && t.align <= 8 && t.size % t.align == 0);
        if (t.kind == schema::Type::Kind::Array) {
            const auto& e = l->type(t.elem);
            REQUIRE(t.count >= 1 && t.size % t.count == 0 && t.size / t.count == e.size);
        }
        std::size_t end = 0;
        for (const auto& f : t.fields) {
            const auto& ft = l->type(f.type);
            REQUIRE(f.offset % ft.align == 0);
            REQUIRE(f.offset >= end);
            REQUIRE(ft.size <= t.size && f.offset <= t.size - ft.size);
            end = f.offset + ft.size;
        }
    }
    // Un esquema variable siempre tiene codificador arrow.
    if (!l->fixed()) {
        const agmen::arrow::Codec codec(*l);
        (void)codec;
    }
    return 0;
}
