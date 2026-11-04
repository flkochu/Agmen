#include "agmen/layout.hpp"

#include <algorithm>
#include <cctype>
#include <set>

#include "agmen/wire.hpp"

namespace agmen::schema {

namespace {

struct PrimInfo {
    std::string_view name;
    Prim prim;
    std::size_t size;
};

constexpr PrimInfo kPrims[] = {
    {"bool", Prim::Bool, 1}, {"i8", Prim::I8, 1},   {"u8", Prim::U8, 1},     {"i16", Prim::I16, 2},
    {"u16", Prim::U16, 2},   {"i32", Prim::I32, 4}, {"u32", Prim::U32, 4},   {"i64", Prim::I64, 8},
    {"u64", Prim::U64, 8},   {"f32", Prim::F32, 4}, {"f64", Prim::F64, 8},   {"time", Prim::Time, 8},
    {"duration", Prim::Duration, 8},
};

constexpr std::uint32_t kMaxArray = 1u << 20;

}  // namespace

std::size_t prim_size(Prim p) {
    for (const auto& i : kPrims) {
        if (i.prim == p) return i.size;
    }
    return 0;
}

std::string_view prim_name(Prim p) {
    for (const auto& i : kPrims) {
        if (i.prim == p) return i.name;
    }
    return "?";
}

// Lector: acepta espacios de más y ';' final, igual que agmen.schema.
struct LayoutParser {
    std::string_view s;
    std::size_t i = 0;
    std::vector<Type>& types;

    [[noreturn]] void fail(const std::string& msg, std::size_t at) const {
        throw SchemaError(msg + "\n  " + std::string(s) + "\n  " + std::string(at, ' ') + "^");
    }
    void skip() {
        while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])) != 0) ++i;
    }
    [[nodiscard]] char peek() {
        skip();
        return i < s.size() ? s[i] : '\0';
    }
    static bool alpha(char c) { return std::isalpha(static_cast<unsigned char>(c)) != 0 || c == '_'; }
    static bool digit(char c) { return c >= '0' && c <= '9'; }

    std::string_view ident(const char* what) {
        skip();
        const std::size_t start = i;
        if (i >= s.size() || !alpha(s[i])) fail(std::string("se esperaba ") + what, i);
        while (i < s.size() && (alpha(s[i]) || digit(s[i]))) ++i;
        return s.substr(start, i - start);
    }
    void expect(char c, const char* what) {
        if (peek() != c) fail(std::string("se esperaba ") + what, i);
        ++i;
    }

    std::uint32_t add(Type t) {
        types.push_back(std::move(t));
        return static_cast<std::uint32_t>(types.size() - 1);
    }

    std::uint32_t parse_struct() {
        Type st;
        st.kind = Type::Kind::Struct;
        st.name = std::string(ident("el nombre del tipo"));
        expect('{', "'{'");
        std::set<std::string> seen;
        std::size_t offset = 0;
        for (;;) {
            if (peek() == '}') {
                if (st.fields.empty()) fail("'" + st.name + "' no tiene campos", i);
                ++i;
                break;
            }
            const std::uint32_t ft = parse_type();
            skip();
            const std::size_t name_at = i;
            const std::string fname(ident("el nombre del campo"));
            if (!seen.insert(fname).second) fail("el campo '" + fname + "' está repetido en '" + st.name + "'", name_at);
            const Type& f = types[ft];
            offset = (offset + f.align - 1) / f.align * f.align;
            st.fields.push_back(Type::Field{fname, ft, offset});
            offset += f.size;
            st.align = std::max(st.align, f.align);
            const char c = peek();
            if (c == ';') {
                ++i;
            } else if (c != '}') {
                fail("se esperaba ';' o '}' después del campo", i);
            }
        }
        st.size = (offset + st.align - 1) / st.align * st.align;
        return add(std::move(st));
    }

    std::uint32_t parse_type() {
        skip();
        const std::size_t start = i;
        const std::string_view name = ident("un tipo (f32, u8, Vec3{...}...)");
        std::uint32_t base = 0;
        if (peek() == '{') {
            i = start;
            base = parse_struct();
        } else {
            const auto* info = std::find_if(std::begin(kPrims), std::end(kPrims),
                                            [&](const PrimInfo& p) { return p.name == name; });
            if (info == std::end(kPrims)) {
                fail("tipo desconocido '" + std::string(name) +
                         "'. Usa bool, i8, u8, i16, u16, i32, u32, i64, u64, f32, f64, time, duration "
                         "o un struct anidado Nombre{...}",
                     start);
            }
            Type t;
            t.kind = Type::Kind::Prim;
            t.prim = info->prim;
            t.size = t.align = info->size;
            base = add(std::move(t));
        }
        std::vector<std::uint32_t> dims;
        while (peek() == '[') {
            ++i;
            skip();
            const std::size_t at = i;
            if (i >= s.size() || !digit(s[i]) || (s[i] == '0' && i + 1 < s.size() && digit(s[i + 1]))) {
                fail("tamaño de arreglo inválido (de 1 a 1048576, sin ceros a la izquierda)", at);
            }
            std::uint64_t n = 0;
            while (i < s.size() && digit(s[i])) {
                n = n * 10 + static_cast<std::uint64_t>(s[i] - '0');
                ++i;
                if (n > kMaxArray) fail("tamaño de arreglo inválido (de 1 a 1048576)", at);
            }
            if (n == 0) fail("tamaño de arreglo inválido (de 1 a 1048576)", at);
            expect(']', "']'");
            dims.push_back(static_cast<std::uint32_t>(n));
        }
        // f32[2][3] = 2 arreglos de f32[3]
        for (auto it = dims.rbegin(); it != dims.rend(); ++it) {
            Type a;
            a.kind = Type::Kind::Array;
            a.count = *it;
            a.elem = base;
            a.size = types[base].size * *it;
            a.align = types[base].align;
            base = add(std::move(a));
        }
        return base;
    }
};

Layout::Layout(std::string_view text) {
    std::string_view body = text;
    while (!body.empty() && std::isspace(static_cast<unsigned char>(body.front())) != 0) body.remove_prefix(1);
    while (!body.empty() && std::isspace(static_cast<unsigned char>(body.back())) != 0) body.remove_suffix(1);
    if (body.starts_with("agmen/")) {
        const std::string_view prefix = "agmen/1";
        if (!body.starts_with(prefix) || (body.size() > prefix.size() &&
                                          std::isspace(static_cast<unsigned char>(body[prefix.size()])) == 0)) {
            const auto end = body.find_first_of(" \t");
            throw SchemaError("versión de esquema no soportada: '" + std::string(body.substr(0, end)) +
                              "' (este Agmen usa 'agmen/1')");
        }
        body.remove_prefix(prefix.size());
    }
    LayoutParser p{body, 0, types_};
    root_ = p.parse_struct();
    if (p.peek() != '\0') p.fail("sobra texto después del esquema", p.i);
    text_ = std::string(wire::kSchemaPrefix) + canonical(root_);
    schema_id_ = wire::fnv1a64(text_);
}

std::string Layout::canonical(std::uint32_t idx) const {
    const Type& t = types_[idx];
    switch (t.kind) {
        case Type::Kind::Prim:
            return std::string(prim_name(t.prim));
        case Type::Kind::Array: {
            std::string dims;
            std::uint32_t base = idx;
            while (types_[base].kind == Type::Kind::Array) {
                dims += "[" + std::to_string(types_[base].count) + "]";
                base = types_[base].elem;
            }
            return canonical(base) + dims;
        }
        case Type::Kind::Struct: {
            std::string out = t.name + "{";
            for (std::size_t k = 0; k < t.fields.size(); ++k) {
                if (k != 0) out += ';';
                out += canonical(t.fields[k].type) + " " + t.fields[k].name;
            }
            return out + "}";
        }
    }
    return {};
}

}  // namespace agmen::schema
