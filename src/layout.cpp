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

// Los límites del protocolo, los mismos que wire::schema::check y Python.
using wire::schema::kMaxArray;
using wire::schema::kMaxDepth;
using wire::schema::kMaxFixedSize;
using wire::schema::kMaxTensorDims;

const std::string kTooBigHint =
    " y un tipo de tamaño fijo mide como mucho 2 GiB (2147483647 bytes): para datos grandes usa bytes, T[] o "
    "tensor<T,N>";
const std::string kTooDeep = "demasiados niveles: un esquema anida como mucho 32 structs y arreglos";

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
    std::vector<std::size_t> height{};  ///< por tipo: niveles de structs y arreglos (un primitivo: 0)
    std::size_t depth = 0;              ///< structs abiertos: la recursión no pasa de kMaxDepth

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

    std::uint32_t add(Type t, std::size_t h) {
        types.push_back(std::move(t));
        height.push_back(h);
        return static_cast<std::uint32_t>(types.size() - 1);
    }

    std::uint32_t parse_struct() {
        skip();
        const std::size_t name_pos = i;
        if (++depth > kMaxDepth) fail(kTooDeep, name_pos);
        Type st;
        st.kind = Type::Kind::Struct;
        st.name = std::string(ident("el nombre del tipo"));
        expect('{', "'{'");
        std::set<std::string> seen;
        // En 64 bits y sin pasar de kMaxFixedSize + 1: sin desbordes, ni en 32 bits.
        std::uint64_t offset = 0;
        std::size_t h = 0;
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
            h = std::max(h, height[ft] + 1);
            if (!f.fixed) st.fixed = false;
            if (st.fixed) {
                offset = (offset + f.align - 1) / f.align * f.align;
                st.fields.push_back(Type::Field{fname, ft, static_cast<std::size_t>(offset)});
                offset = std::min<std::uint64_t>(offset + f.size, kMaxFixedSize + 1);
                st.align = std::max(st.align, f.align);
            } else {
                st.fields.push_back(Type::Field{fname, ft, 0});
            }
            const char c = peek();
            if (c == ';') {
                ++i;
            } else if (c != '}') {
                fail("se esperaba ';' o '}' después del campo", i);
            }
        }
        if (h > kMaxDepth) fail(kTooDeep, name_pos);
        --depth;
        if (st.fixed) {
            const std::uint64_t size = (offset + st.align - 1) / st.align * st.align;
            if (size > kMaxFixedSize) {
                fail("'" + st.name + "' mediría " + (offset > kMaxFixedSize ? "más de 2 GiB" : std::to_string(size) + " bytes") +
                         kTooBigHint,
                     name_pos);
            }
            st.size = static_cast<std::size_t>(size);
        } else {  // variable: sin offsets (va en arrow)
            st.size = 0;
            st.align = 1;
            for (auto& fl : st.fields) fl.offset = 0;
        }
        return add(std::move(st), h);
    }

    static bool is_number(Prim p) { return p != Prim::Bool && p != Prim::Time && p != Prim::Duration; }

    std::uint32_t parse_tensor() {  // ya se leyó "tensor"
        if (peek() != '<') fail("se esperaba '<' (se escribe tensor<f32,2>)", i);
        ++i;
        skip();
        const std::size_t at = i;
        const std::string_view e = ident("el tipo de los elementos (ej. tensor<f32,2>)");
        const auto* info = std::find_if(std::begin(kPrims), std::end(kPrims),
                                        [&](const PrimInfo& p) { return p.name == e; });
        if (info == std::end(kPrims) || !is_number(info->prim)) {
            fail("un tensor solo puede ser de números (i8, u8, i16, u16, i32, u32, i64, u64, f32, f64), no de '" +
                     std::string(e) + "'",
                 at);
        }
        if (peek() != ',') fail("se esperaba ',' y el número de dimensiones (ej. tensor<f32,2>)", i);
        ++i;
        skip();
        const std::size_t nat = i;
        std::uint32_t n = 0;
        if (i >= s.size() || !digit(s[i]) || s[i] == '0') fail("número de dimensiones inválido (de 1 a 32)", nat);
        while (i < s.size() && digit(s[i])) {
            n = n * 10 + static_cast<std::uint32_t>(s[i] - '0');
            ++i;
            if (n > kMaxTensorDims) fail("número de dimensiones inválido (de 1 a 32)", nat);
        }
        expect('>', "'>'");
        Type t;
        t.kind = Type::Kind::Tensor;
        t.prim = info->prim;
        t.count = n;
        t.fixed = false;
        return add(std::move(t), 0);
    }

    std::uint32_t parse_type() {
        skip();
        const std::size_t start = i;
        const std::string_view name = ident("un tipo (f32, u8, Vec3{...}...)");
        std::uint32_t base = 0;
        if (peek() == '{') {
            i = start;
            base = parse_struct();
        } else if (name == "string" || name == "bytes") {
            Type t;
            t.kind = name == "string" ? Type::Kind::String : Type::Kind::Bytes;
            t.fixed = false;
            base = add(std::move(t), 0);
        } else if (name == "tensor") {
            base = parse_tensor();
        } else if (name == "optional") {
            fail("'optional' todavía no existe en Agmen: usa un valor que signifique 'no hay' (ej. -1) o un "
                 "bool al lado",
                 start);
        } else if (name == "str") {
            fail("tipo 'str' no existe en Agmen: usa 'string'", start);
        } else {
            const auto* info = std::find_if(std::begin(kPrims), std::end(kPrims),
                                            [&](const PrimInfo& p) { return p.name == name; });
            if (info == std::end(kPrims)) {
                fail("tipo desconocido '" + std::string(name) +
                         "'. Usa bool, i8, u8, i16, u16, i32, u32, i64, u64, f32, f64, time, duration, "
                         "string, bytes, tensor<T,N> o un struct anidado Nombre{...}",
                     start);
            }
            Type t;
            t.kind = Type::Kind::Prim;
            t.prim = info->prim;
            t.size = t.align = info->size;
            base = add(std::move(t), 0);
        }
        struct Dim {
            std::uint32_t n;  // 0 = T[] (tamaño variable)
            std::size_t at;
        };
        std::vector<Dim> dims;
        while (peek() == '[') {
            if (height[base] + dims.size() + 1 > kMaxDepth) fail(kTooDeep, i);
            const std::size_t open = i;
            ++i;
            if (peek() == ']') {
                ++i;
                dims.push_back({0, open});
                continue;
            }
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
            dims.push_back({static_cast<std::uint32_t>(n), at});
        }
        // f32[2][3] = 2 arreglos de f32[3]; f32[][3] = lista de f32[3]
        for (auto it = dims.rbegin(); it != dims.rend(); ++it) {
            Type a;
            a.elem = base;
            if (it->n == 0) {
                a.kind = Type::Kind::List;
                a.fixed = false;
            } else {
                a.kind = Type::Kind::Array;
                a.count = it->n;
                a.fixed = types[base].fixed;
                if (a.fixed && types[base].size > kMaxFixedSize / it->n) {
                    fail("el arreglo mediría más de 2 GiB" + kTooBigHint, it->at);
                }
                a.size = a.fixed ? types[base].size * it->n : 0;
                a.align = a.fixed ? types[base].align : 1;
            }
            base = add(std::move(a), height[base] + 1);
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
    LayoutParser p{body, 0, types_, {}, 0};
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
        case Type::Kind::String:
            return "string";
        case Type::Kind::Bytes:
            return "bytes";
        case Type::Kind::Tensor:
            return "tensor<" + std::string(prim_name(t.prim)) + "," + std::to_string(t.count) + ">";
        case Type::Kind::Array:
        case Type::Kind::List: {
            std::string dims;
            std::uint32_t base = idx;
            while (types_[base].kind == Type::Kind::Array || types_[base].kind == Type::Kind::List) {
                dims += types_[base].kind == Type::Kind::Array ? "[" + std::to_string(types_[base].count) + "]" : "[]";
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
