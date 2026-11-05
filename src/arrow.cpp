// La codificación arrow (RFC 0008). La referencia es python/agmen/arrow.py:
// mismo orden de buffers, mismas reglas de alineación y el mismo acomodo del
// flatbuffer, para que los tres lenguajes den los mismos bytes.
#include "agmen/arrow.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <deque>
#include <limits>

namespace agmen::arrow {

using schema::Prim;
using schema::SchemaError;
using schema::Type;
using wire::ByteView;
using wire::Bytes;
using wire::WireError;

namespace {

constexpr std::size_t kBigBuffer = 64;
constexpr std::int16_t kMetadataV5 = 4;
constexpr std::uint8_t kHeaderRecordBatch = 3;

std::size_t align_up(std::size_t n, std::size_t a) { return (n + a - 1) / a * a; }

[[noreturn]] void bad(const std::string& msg) { throw WireError(WireError::Kind::BadArrow, "arrow: " + msg); }

template <class T>
void put(std::uint8_t* at, T v) {
    std::memcpy(at, &v, sizeof v);
}
template <class T>
T get(const std::uint8_t* at) {
    T v;
    std::memcpy(&v, at, sizeof v);
    return v;
}

bool valid_utf8(ByteView s) {
    std::size_t i = 0;
    while (i < s.size()) {
        const std::uint8_t c = s[i];
        if (c < 0x80) {
            ++i;
            continue;
        }
        std::size_t n = 0;  // bytes de continuación
        std::uint32_t cp = 0;
        if ((c & 0xE0) == 0xC0) {
            n = 1;
            cp = c & 0x1Fu;
        } else if ((c & 0xF0) == 0xE0) {
            n = 2;
            cp = c & 0x0Fu;
        } else if ((c & 0xF8) == 0xF0) {
            n = 3;
            cp = c & 0x07u;
        } else {
            return false;
        }
        if (n >= s.size() - i) return false;
        for (std::size_t k = 1; k <= n; ++k) {
            if ((s[i + k] & 0xC0) != 0x80) return false;
            cp = (cp << 6) | (s[i + k] & 0x3Fu);
        }
        if ((n == 1 && cp < 0x80) || (n == 2 && cp < 0x800) || (n == 3 && cp < 0x10000) || cp > 0x10FFFF ||
            (cp >= 0xD800 && cp <= 0xDFFF)) {
            return false;
        }
        i += n + 1;
    }
    return true;
}

bool is_number(Prim p) { return p != Prim::Bool; }

/// Elementos de un tensor con forma `shape` (dimensiones ya revisadas ≥ 0) y
/// elementos de `elem_size` bytes, con la cuenta exacta. false si la forma es
/// imposible: como en NumPy (y en Python y agmen.js), las dimensiones, contando
/// un 0 como 1, por el tamaño del elemento deben caber en 63 bits, aunque haya
/// un 0 y el tensor no traiga datos.
template <class Shape>
bool shape_elems(const Shape& shape, std::size_t elem_size, std::uint64_t& out) {
    constexpr auto kMax = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    std::uint64_t nominal = elem_size;
    bool zero = false;
    for (const auto d : shape) {
        const auto u = static_cast<std::uint64_t>(d);
        if (u == 0) {
            zero = true;
            continue;
        }
        if (nominal > kMax / u) return false;
        nominal *= u;
    }
    out = zero ? 0 : nominal / elem_size;
    return true;
}

const char* prim_label(Prim p) {
    static thread_local std::string s;
    s = std::string(schema::prim_name(p));
    return s.c_str();
}

}  // namespace

// ---------------------------------------------------------------------------
// Árbol de Arrow
// ---------------------------------------------------------------------------

Codec::Codec(const schema::Layout& layout) : layout_(&layout) {
    if (layout.fixed()) {
        throw SchemaError("'" + layout.name() + "' es de tamaño fijo: viaja en compact, no en arrow");
    }
    root_ = build(layout.root());
}

std::uint32_t Codec::build(std::uint32_t idx) {
    const Type& t = layout_->type(idx);
    Node n;
    switch (t.kind) {
        case Type::Kind::Prim:
            n.kind = t.prim == Prim::Bool ? Node::Kind::Bool : Node::Kind::Prim;
            n.prim = t.prim;
            n.size = schema::prim_size(t.prim);
            break;
        case Type::Kind::String: n.kind = Node::Kind::Utf8; break;
        case Type::Kind::Bytes: n.kind = Node::Kind::Binary; break;
        case Type::Kind::List: n.kind = Node::Kind::List; n.children.push_back(build(t.elem)); break;
        case Type::Kind::Tensor: {
            n.kind = Node::Kind::Tensor;
            n.prim = t.prim;
            n.size = schema::prim_size(t.prim);
            n.ndim = t.count;
            break;
        }
        case Type::Kind::Array: {
            std::vector<std::uint32_t> dims;
            std::uint32_t base = idx;
            while (layout_->type(base).kind == Type::Kind::Array) {
                dims.push_back(layout_->type(base).count);
                base = layout_->type(base).elem;
            }
            const Type& b = layout_->type(base);
            n.kind = Node::Kind::Fsl;
            if (b.kind == Type::Kind::Prim && is_number(b.prim)) {  // números: un FixedSizeList (fixed_shape_tensor)
                n.dims = dims;
                n.n = 1;
                for (auto d : dims) n.n *= d;
                n.children.push_back(build(base));
            } else {
                n.dims.assign(1, t.count);
                n.n = t.count;
                n.children.push_back(build(t.elem));
            }
            break;
        }
        case Type::Kind::Struct:
            n.kind = Node::Kind::Struct;
            for (const auto& f : t.fields) {
                n.names.push_back(f.name);
                n.children.push_back(build(f.type));
            }
            break;
    }
    nodes_.push_back(std::move(n));
    return static_cast<std::uint32_t>(nodes_.size() - 1);
}

// ---------------------------------------------------------------------------
// Valores → buffers
// ---------------------------------------------------------------------------

struct Writer {
    const Codec& c;
    std::vector<std::pair<std::int64_t, std::int64_t>> nodes;
    std::vector<ByteView> buffers;
    std::deque<Bytes> owned;  // buffers que se arman aquí (offsets, bits, números sueltos)

    ByteView keep(Bytes b) {
        owned.push_back(std::move(b));
        return owned.back();
    }
    void node(std::size_t len) { nodes.emplace_back(static_cast<std::int64_t>(len), 0); }
    void buf(ByteView b) { buffers.push_back(b); }
    void validity() { buffers.emplace_back(); }

    static std::string at(const std::string& where, std::size_t k, std::size_t n) {
        return n > 1 ? where + "[" + std::to_string(k) + "]" : where;
    }

    void scalar_into(Prim p, const Value& v, const std::string& where, std::uint8_t* out) const {
        const auto need_number = [&] {
            if (v.kind != Value::Kind::Int && v.kind != Value::Kind::UInt && v.kind != Value::Kind::Float &&
                v.kind != Value::Kind::Bool) {
                throw SchemaError("'" + where + "' debe ser un número");
            }
        };
        need_number();
        const auto range = [&](auto lo, auto hi) {
            using L = decltype(lo);
            if (v.kind == Value::Kind::Float) {
                if (!(v.f >= static_cast<double>(lo) && v.f <= static_cast<double>(hi)) || std::trunc(v.f) != v.f) {
                    throw SchemaError("'" + where + "' debe ser un entero de " + std::to_string(lo) + " a " +
                                      std::to_string(hi));
                }
                return static_cast<L>(v.f);
            }
            if (v.kind == Value::Kind::UInt) {
                if (v.u > static_cast<std::uint64_t>(hi)) {
                    throw SchemaError("'" + where + "' no cabe en " + prim_label(p) + " (de " + std::to_string(lo) +
                                      " a " + std::to_string(hi) + ")");
                }
                return static_cast<L>(v.u);
            }
            const std::int64_t x = v.kind == Value::Kind::Bool ? (v.b ? 1 : 0) : v.i;
            if constexpr (std::is_unsigned_v<L>) {
                if (x < 0 || static_cast<std::uint64_t>(x) > static_cast<std::uint64_t>(hi)) {
                    throw SchemaError("'" + where + "' no cabe en " + prim_label(p) + " (de 0 a " + std::to_string(hi) +
                                      ")");
                }
            } else {
                if (x < static_cast<std::int64_t>(lo) || x > static_cast<std::int64_t>(hi)) {
                    throw SchemaError("'" + where + "' no cabe en " + prim_label(p) + " (de " + std::to_string(lo) +
                                      " a " + std::to_string(hi) + ")");
                }
            }
            return static_cast<L>(x);
        };
        switch (p) {
            case Prim::I8: put(out, range(std::int8_t{-128}, std::int8_t{127})); break;
            case Prim::U8: put(out, range(std::uint8_t{0}, std::uint8_t{255})); break;
            case Prim::I16: put(out, range(std::numeric_limits<std::int16_t>::min(), std::numeric_limits<std::int16_t>::max())); break;
            case Prim::U16: put(out, range(std::uint16_t{0}, std::numeric_limits<std::uint16_t>::max())); break;
            case Prim::I32: put(out, range(std::numeric_limits<std::int32_t>::min(), std::numeric_limits<std::int32_t>::max())); break;
            case Prim::U32: put(out, range(std::uint32_t{0}, std::numeric_limits<std::uint32_t>::max())); break;
            case Prim::I64:
            case Prim::Time:
            case Prim::Duration: {
                std::int64_t x = 0;
                if (v.kind == Value::Kind::UInt) {
                    if (v.u > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
                        throw SchemaError("'" + where + "' no cabe en " + prim_label(p));
                    }
                    x = static_cast<std::int64_t>(v.u);
                } else if (v.kind == Value::Kind::Float) {
                    // [-2^63, 2^63): fuera de ahí (o NaN) convertir es comportamiento indefinido
                    if (!(v.f >= -0x1p63 && v.f < 0x1p63) || std::trunc(v.f) != v.f) {
                        throw SchemaError("'" + where + "' debe ser un entero que quepa en " + prim_label(p));
                    }
                    x = static_cast<std::int64_t>(v.f);
                } else {
                    x = v.kind == Value::Kind::Bool ? (v.b ? 1 : 0) : v.i;
                }
                put(out, x);
                break;
            }
            case Prim::U64: {
                std::uint64_t x = 0;
                if (v.kind == Value::Kind::Int) {
                    if (v.i < 0) throw SchemaError("'" + where + "' no cabe en u64 (no puede ser negativo)");
                    x = static_cast<std::uint64_t>(v.i);
                } else if (v.kind == Value::Kind::Float) {
                    if (!(v.f >= 0 && v.f < 0x1p64) || std::trunc(v.f) != v.f) {  // [0, 2^64)
                        throw SchemaError("'" + where + "' debe ser un entero que quepa en u64");
                    }
                    x = static_cast<std::uint64_t>(v.f);
                } else {
                    x = v.kind == Value::Kind::Bool ? (v.b ? 1u : 0u) : v.u;
                }
                put(out, x);
                break;
            }
            case Prim::F32: {
                const double d = v.as<double>();
                if (std::isfinite(d) && std::fabs(d) > static_cast<double>(std::numeric_limits<float>::max())) {
                    throw SchemaError("'" + where + "' no cabe en f32");
                }
                put(out, static_cast<float>(d));
                break;
            }
            case Prim::F64: put(out, v.as<double>()); break;
            case Prim::Bool: put(out, static_cast<std::uint8_t>(v.as<bool>() ? 1 : 0)); break;
        }
    }

    /// Los números de varias filas seguidos (cada fila: Raw, o List de escalares, anidada si es T[N][M]).
    ByteView numbers(const Codec::Node& prim_node, const std::vector<const Value*>& rows, const std::string& where,
                     std::size_t& total) {
        const std::size_t sz = prim_node.size;
        if (rows.size() == 1 && (rows[0]->kind == Value::Kind::Raw || rows[0]->kind == Value::Kind::Tensor)) {
            const auto b = rows[0]->bytes();
            if (b.size() % sz != 0) throw SchemaError("'" + where + "': los bytes no son un número entero de valores");
            total = b.size() / sz;
            return b;  // sin copiar
        }
        Bytes out;
        total = 0;
        for (std::size_t k = 0; k < rows.size(); ++k) {
            const Value& r = *rows[k];
            if (r.kind == Value::Kind::Raw || r.kind == Value::Kind::Tensor) {
                const auto b = r.bytes();
                if (b.size() % sz != 0) throw SchemaError("'" + where + "': los bytes no son un número entero de valores");
                out.insert(out.end(), b.begin(), b.end());
                total += b.size() / sz;
            } else {
                std::vector<const Value*> flat;
                flatten(r, flat);
                const std::size_t start = out.size();
                out.resize(start + flat.size() * sz);
                for (std::size_t j = 0; j < flat.size(); ++j) {
                    scalar_into(prim_node.prim, *flat[j], at(at(where, k, rows.size()), j, flat.size()),
                                out.data() + start + j * sz);
                }
                total += flat.size();
            }
        }
        return keep(std::move(out));
    }

    static void flatten(const Value& v, std::vector<const Value*>& out) {
        if (v.kind == Value::Kind::List) {
            for (const auto& x : v.items) flatten(x, out);
        } else {
            out.push_back(&v);
        }
    }

    static std::size_t length_of(const Value& v, std::size_t elem_size, const std::string& where) {
        if (v.kind == Value::Kind::List) return v.items.size();
        if (v.kind == Value::Kind::Raw && elem_size != 0) return v.bytes().size() / elem_size;
        throw SchemaError("'" + where + "' debe ser una lista o un arreglo");
    }

    /// ¿v tiene la forma dims? Si no, (esperado, recibido) del primer nivel que falla.
    static bool shape_ok(const Value& v, const std::vector<std::uint32_t>& dims, std::size_t from, std::size_t elem_size,
                         std::size_t& want, std::size_t& got) {
        if (v.kind == Value::Kind::Raw) {
            std::size_t n = 1;
            for (std::size_t k = from; k < dims.size(); ++k) n *= dims[k];
            want = n;
            got = elem_size ? v.bytes().size() / elem_size : 0;
            return elem_size != 0 && v.bytes().size() == n * elem_size;
        }
        if (v.kind != Value::Kind::List) {
            want = dims[from];
            got = 1;
            return false;
        }
        if (v.items.size() != dims[from]) {
            want = dims[from];
            got = v.items.size();
            return false;
        }
        if (from + 1 < dims.size()) {
            for (const auto& x : v.items) {
                if (!shape_ok(x, dims, from + 1, elem_size, want, got)) return false;
            }
        }
        return true;
    }

    void offsets_buf(const std::vector<std::size_t>& lens, const std::string& where) {
        Bytes b(4 * (lens.size() + 1));
        std::size_t pos = 0;
        put(b.data(), std::int32_t{0});
        for (std::size_t k = 0; k < lens.size(); ++k) {
            pos += lens[k];
            if (pos > static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())) {
                throw SchemaError("'" + where + "' es demasiado grande: el máximo de un campo es 2 GiB");
            }
            put(b.data() + 4 * (k + 1), static_cast<std::int32_t>(pos));
        }
        buf(keep(std::move(b)));
    }

    void write(std::uint32_t ni, const std::vector<const Value*>& values, const std::string& where) {
        const Codec::Node& n = c.nodes_[ni];
        const std::size_t len = values.size();
        node(len);
        validity();
        switch (n.kind) {
            case Codec::Node::Kind::Prim: {
                Bytes b(len * n.size);
                for (std::size_t k = 0; k < len; ++k) scalar_into(n.prim, *values[k], at(where, k, len), b.data() + k * n.size);
                buf(keep(std::move(b)));
                break;
            }
            case Codec::Node::Kind::Bool: {
                Bytes b(align_up(len, 8) / 8);
                for (std::size_t k = 0; k < len; ++k) {
                    if (values[k]->as<bool>()) b[k >> 3] = static_cast<std::uint8_t>(b[k >> 3] | (1u << (k & 7)));
                }
                buf(keep(std::move(b)));
                break;
            }
            case Codec::Node::Kind::Utf8:
            case Codec::Node::Kind::Binary: {
                std::vector<std::size_t> lens;
                for (std::size_t k = 0; k < len; ++k) {
                    if (values[k]->kind != Value::Kind::Blob) {
                        throw SchemaError("'" + at(where, k, len) + "' es " +
                                          (n.kind == Codec::Node::Kind::Utf8 ? "string y debe ser texto" : "bytes y debe ser bytes"));
                    }
                    if (n.kind == Codec::Node::Kind::Utf8 && !valid_utf8(values[k]->bytes())) {
                        throw SchemaError("'" + at(where, k, len) + "' no es UTF-8 válido");
                    }
                    lens.push_back(values[k]->bytes().size());
                }
                offsets_buf(lens, where);
                if (len == 1) {
                    buf(values[0]->bytes());  // sin copiar (una imagen, un JPEG)
                } else {
                    Bytes b;
                    for (const auto* v : values) b.insert(b.end(), v->bytes().begin(), v->bytes().end());
                    buf(keep(std::move(b)));
                }
                break;
            }
            case Codec::Node::Kind::List: {
                const auto& child = c.nodes_[n.children[0]];
                const std::size_t esz = child.kind == Codec::Node::Kind::Prim ? child.size : 0;
                std::vector<std::size_t> lens;
                for (std::size_t k = 0; k < len; ++k) lens.push_back(length_of(*values[k], esz, at(where, k, len)));
                offsets_buf(lens, where);
                if (child.kind == Codec::Node::Kind::Prim) {
                    std::size_t total = 0;
                    const ByteView data = numbers(child, values, where, total);
                    node(total);
                    validity();
                    buf(data);
                } else {
                    std::vector<const Value*> flat;
                    for (const auto* v : values) {
                        if (v->kind != Value::Kind::List) throw SchemaError("'" + where + "' debe ser una lista");
                        for (const auto& x : v->items) flat.push_back(&x);
                    }
                    write(n.children[0], flat, where + "[]");
                }
                break;
            }
            case Codec::Node::Kind::Fsl: {
                const auto& child = c.nodes_[n.children[0]];
                const std::size_t esz = child.kind == Codec::Node::Kind::Prim ? child.size : 0;
                for (std::size_t k = 0; k < len; ++k) {
                    std::size_t want = 0;
                    std::size_t got = 0;
                    if (!shape_ok(*values[k], n.dims, 0, esz, want, got)) {
                        throw SchemaError("'" + where + "' debe tener " + std::to_string(want) + " elementos, tiene " +
                                          std::to_string(got));
                    }
                }
                if (child.kind == Codec::Node::Kind::Prim) {
                    std::size_t total = 0;
                    const ByteView data = numbers(child, values, where, total);
                    node(total);
                    validity();
                    buf(data);
                } else {
                    std::vector<const Value*> flat;
                    for (const auto* v : values) {
                        for (const auto& x : v->items) flat.push_back(&x);
                    }
                    write(n.children[0], flat, where);
                }
                break;
            }
            case Codec::Node::Kind::Struct: {
                for (std::size_t k = 0; k < len; ++k) {
                    if (values[k]->kind != Value::Kind::Struct || values[k]->items.size() != n.names.size()) {
                        throw SchemaError("'" + (where.empty() ? std::string("el mensaje") : where) + "' debe traer " +
                                          std::to_string(n.names.size()) + " campos");
                    }
                }
                for (std::size_t f = 0; f < n.names.size(); ++f) {
                    std::vector<const Value*> col;
                    col.reserve(len);
                    for (const auto* v : values) col.push_back(&v->items[f]);
                    write(n.children[f], col, where.empty() ? n.names[f] : where + "." + n.names[f]);
                }
                break;
            }
            case Codec::Node::Kind::Tensor: {
                std::vector<std::size_t> lens;
                Bytes shapes;
                for (std::size_t k = 0; k < len; ++k) {
                    const Value& v = *values[k];
                    const std::string w = at(where, k, len);
                    if (v.kind != Value::Kind::Tensor) {
                        throw SchemaError("'" + w + "' es tensor<" + prim_label(n.prim) + "," + std::to_string(n.ndim) +
                                          ">: pásale un agmen::Tensor");
                    }
                    if (v.shape.size() != n.ndim) {
                        throw SchemaError("'" + w + "' es tensor<" + prim_label(n.prim) + "," + std::to_string(n.ndim) +
                                          "> y llegó con " + std::to_string(v.shape.size()) + " dimensiones");
                    }
                    for (auto d : v.shape) {
                        if (d < 0) throw SchemaError("'" + w + "': forma inválida");
                        put(&*shapes.insert(shapes.end(), 4, 0), d);
                    }
                    std::uint64_t want = 0;
                    if (!shape_elems(v.shape, n.size, want)) {
                        throw SchemaError("'" + w + "': su forma tiene más elementos de los que se pueden contar");
                    }
                    const std::size_t got = v.bytes().size() / n.size;
                    if (want != got || v.bytes().size() % n.size != 0) {
                        throw SchemaError("'" + w + "' tiene " + std::to_string(want) + " elementos según su forma y trae " +
                                          std::to_string(got) + " datos");
                    }
                    lens.push_back(got);
                }
                node(len);  // data: List<T>
                validity();
                offsets_buf(lens, where);
                Codec::Node elem;
                elem.kind = Codec::Node::Kind::Prim;
                elem.prim = n.prim;
                elem.size = n.size;
                std::size_t total = 0;
                const ByteView data = numbers(elem, values, where, total);
                node(total);
                validity();
                buf(data);
                node(len);  // shape: FixedSizeList<i32>[ndim]
                validity();
                node(len * n.ndim);
                validity();
                buf(keep(std::move(shapes)));
                break;
            }
        }
    }
};

namespace {

/// Message{version=V5, header=RecordBatch{length, nodes, buffers}, bodyLength}, con el
/// mismo acomodo que python/agmen/arrow.py:_metadata.
Bytes metadata(std::int64_t body_len, std::int64_t rows, const std::vector<std::pair<std::int64_t, std::int64_t>>& nodes,
               const std::vector<std::pair<std::int64_t, std::int64_t>>& bufs) {
    const std::size_t e = 80 + 16 * nodes.size();
    const std::size_t bv = e + 4;
    Bytes out(e + 8 + 16 * bufs.size(), 0);
    std::uint8_t* o = out.data();
    put(o + 0, std::uint32_t{16});
    const std::array<std::uint16_t, 6> vt1{12, 20, 16, 18, 4, 8};
    std::memcpy(o + 4, vt1.data(), sizeof vt1);
    put(o + 16, std::int32_t{16 - 4});
    put(o + 20, std::uint32_t{48 - 20});
    put(o + 24, body_len);
    put(o + 32, kMetadataV5);
    put(o + 34, kHeaderRecordBatch);
    const std::array<std::uint16_t, 5> vt2{10, 24, 8, 4, 16};
    std::memcpy(o + 36, vt2.data(), sizeof vt2);
    put(o + 48, std::int32_t{48 - 36});
    put(o + 52, std::uint32_t{76 - 52});
    put(o + 56, rows);
    put(o + 64, static_cast<std::uint32_t>(bv - 64));
    put(o + 76, static_cast<std::uint32_t>(nodes.size()));
    for (std::size_t k = 0; k < nodes.size(); ++k) {
        put(o + 80 + 16 * k, nodes[k].first);
        put(o + 88 + 16 * k, nodes[k].second);
    }
    put(o + bv, static_cast<std::uint32_t>(bufs.size()));
    for (std::size_t k = 0; k < bufs.size(); ++k) {
        put(o + e + 8 + 16 * k, bufs[k].first);
        put(o + e + 16 + 16 * k, bufs[k].second);
    }
    return out;
}

}  // namespace

void Codec::encode(const Value& row, Bytes& out) const {
    Writer w{*this, {}, {}, {}};
    const Node& root = nodes_[root_];
    if (row.kind != Value::Kind::Struct || row.items.size() != root.names.size()) {
        throw SchemaError("'" + layout_->name() + "' debe traer " + std::to_string(root.names.size()) + " campos");
    }
    for (std::size_t f = 0; f < root.names.size(); ++f) w.write(root.children[f], {&row.items[f]}, root.names[f]);

    std::vector<std::pair<std::int64_t, std::int64_t>> places;
    std::size_t pos = 0;
    for (const auto& b : w.buffers) {
        pos = align_up(pos, b.size() >= kBigBuffer ? kBigBuffer : 8);
        places.emplace_back(static_cast<std::int64_t>(pos), static_cast<std::int64_t>(b.size()));
        pos += b.size();
    }
    const std::size_t body_len = align_up(pos, 8);
    const Bytes meta = metadata(static_cast<std::int64_t>(body_len), 1, w.nodes, places);
    const std::size_t meta_len = align_up(8 + meta.size(), 64) - 8;
    const std::size_t total = 8 + meta_len + body_len;
    if (total > wire::max_message()) {
        throw WireError(WireError::Kind::TooBig, wire::too_big_text(layout_->name(), total));
    }
    const std::size_t start = out.size();
    out.resize(start + total, 0);
    std::uint8_t* o = out.data() + start;
    put(o, std::uint32_t{0xFFFFFFFF});
    put(o + 4, static_cast<std::int32_t>(meta_len));
    std::memcpy(o + 8, meta.data(), meta.size());
    std::uint8_t* body = o + 8 + meta_len;
    for (std::size_t k = 0; k < w.buffers.size(); ++k) {
        if (!w.buffers[k].empty()) {
            std::memcpy(body + places[k].first, w.buffers[k].data(), w.buffers[k].size());
        }
    }
}

Bytes Codec::encode(const Value& row) const {
    Bytes out;
    encode(row, out);
    return out;
}

// ---------------------------------------------------------------------------
// Buffers → valores
// ---------------------------------------------------------------------------

namespace {

struct FB {
    ByteView b;
    void need(std::size_t p, std::size_t n) const {
        if (p > b.size() || n > b.size() - p) bad("los metadatos apuntan fuera del mensaje");
    }
    template <class T>
    T at(std::size_t p) const {
        need(p, sizeof(T));
        return get<T>(b.data() + p);
    }
    std::size_t deref(std::size_t p) const { return p + at<std::uint32_t>(p); }
    struct Table {
        const FB* fb;
        std::size_t vt;
        std::uint16_t size;
        [[nodiscard]] std::uint16_t field(std::size_t k) const {
            const std::size_t o = 4 + 2 * k;
            return o + 2 > size ? std::uint16_t{0} : fb->at<std::uint16_t>(vt + o);
        }
    };
    Table table(std::size_t pos) const {
        const auto soff = at<std::int32_t>(pos);
        const auto vt = static_cast<std::int64_t>(pos) - soff;
        if (vt < 0) bad("vtable inválida");
        const auto size = at<std::uint16_t>(static_cast<std::size_t>(vt));
        if (size < 4 || size % 2 != 0) bad("vtable inválida");
        need(static_cast<std::size_t>(vt), size);
        return {this, static_cast<std::size_t>(vt), size};
    }
};

struct Batch {
    std::int64_t rows = 0;
    std::vector<std::pair<std::int64_t, std::int64_t>> nodes;
    std::vector<std::pair<std::int64_t, std::int64_t>> bufs;
    ByteView body;
};

Batch read_metadata(ByteView payload) {
    if (payload.size() < 8) bad("el payload mide " + std::to_string(payload.size()) + " bytes");
    if (get<std::uint32_t>(payload.data()) != 0xFFFFFFFFu) {
        bad("falta la marca de continuación 0xFFFFFFFF (¿formato de Arrow anterior a 0.15?)");
    }
    const auto meta_len = get<std::int32_t>(payload.data() + 4);
    if (meta_len <= 0 || static_cast<std::size_t>(meta_len) > payload.size() - 8) {
        bad("metadatos de " + std::to_string(meta_len) + " bytes en un payload de " + std::to_string(payload.size()));
    }
    const FB fb{payload.subspan(8, static_cast<std::size_t>(meta_len))};
    const std::size_t msg = fb.deref(0);
    const auto f = fb.table(msg);
    const std::int16_t version = f.field(0) ? fb.at<std::int16_t>(msg + f.field(0)) : 0;
    if (version < 3) bad("versión de metadatos " + std::to_string(version + 1) + " no soportada (se usa V4 o V5)");
    const std::uint8_t htype = f.field(1) ? fb.at<std::uint8_t>(msg + f.field(1)) : 0;
    if (htype != kHeaderRecordBatch || f.field(2) == 0) bad("se esperaba un RecordBatch");
    const std::int64_t body_len = f.field(3) ? fb.at<std::int64_t>(msg + f.field(3)) : 0;
    const std::size_t rb = fb.deref(msg + f.field(2));
    const auto g = fb.table(rb);
    Batch out;
    out.rows = g.field(0) ? fb.at<std::int64_t>(rb + g.field(0)) : 0;
    if (g.field(3) != 0) bad("RecordBatch comprimido (LZ4/ZSTD): Agmen todavía no lo lee");
    const auto vec = [&](std::size_t k) {
        std::vector<std::pair<std::int64_t, std::int64_t>> v;
        if (g.field(k) == 0) return v;
        const std::size_t p = fb.deref(rb + g.field(k));
        const auto count = fb.at<std::uint32_t>(p);
        fb.need(p + 4, std::size_t{16} * count);
        v.reserve(count);
        for (std::size_t j = 0; j < count; ++j) {
            v.emplace_back(fb.at<std::int64_t>(p + 4 + 16 * j), fb.at<std::int64_t>(p + 12 + 16 * j));
        }
        return v;
    };
    out.nodes = vec(1);
    out.bufs = vec(2);
    const std::size_t body_start = 8 + static_cast<std::size_t>(meta_len);
    if (body_len < 0 || static_cast<std::size_t>(body_len) > payload.size() - body_start) {
        bad("el cuerpo mide " + std::to_string(body_len) + " bytes y solo llegaron " +
            std::to_string(payload.size() - body_start));
    }
    out.body = payload.subspan(body_start, static_cast<std::size_t>(body_len));
    if (out.rows < 0) bad("número de filas inválido");
    return out;
}

}  // namespace

struct Reader {
    const Codec& c;
    const Batch& bt;
    std::size_t i_node = 0;
    std::size_t i_buf = 0;

    /// Lo más que puede medir una columna: cada valor ocupa al menos un bit del cuerpo.
    [[nodiscard]] std::size_t max_len() const { return bt.body.size() * 8 + 64; }

    std::size_t node() {
        if (i_node >= bt.nodes.size()) bad("el RecordBatch tiene menos columnas que el esquema");
        const auto [len, nulls] = bt.nodes[i_node++];
        if (nulls != 0) bad("llegaron valores nulos y los esquemas de Agmen no los tienen");
        if (len < 0 || static_cast<std::uint64_t>(len) > max_len()) bad("una columna con un largo inválido");
        return static_cast<std::size_t>(len);
    }
    void node(std::size_t want) {
        const std::size_t got = node();
        if (got != want) bad("una columna mide " + std::to_string(got) + " y debía medir " + std::to_string(want));
    }
    ByteView buf(std::size_t need = 0) {
        if (i_buf >= bt.bufs.size()) bad("el RecordBatch tiene menos buffers que el esquema");
        const auto [off, len] = bt.bufs[i_buf++];
        if (off < 0 || len < 0 || static_cast<std::uint64_t>(off) > bt.body.size() ||
            static_cast<std::uint64_t>(len) > bt.body.size() - static_cast<std::size_t>(off)) {
            bad("un buffer se sale del cuerpo");
        }
        if (static_cast<std::size_t>(len) < need) {
            bad("un buffer mide " + std::to_string(len) + " bytes y se necesitan " + std::to_string(need));
        }
        return bt.body.subspan(static_cast<std::size_t>(off), static_cast<std::size_t>(len));
    }
    std::vector<std::size_t> offsets(std::size_t len) {
        if (len > bt.body.size()) bad("una columna con un largo inválido");
        const ByteView b = buf(4 * (len + 1));
        std::vector<std::size_t> out(len + 1);
        std::int32_t prev = get<std::int32_t>(b.data());
        if (prev < 0) bad("offsets negativos");
        for (std::size_t j = 0; j <= len; ++j) {
            const auto o = get<std::int32_t>(b.data() + 4 * j);
            if (o < prev) bad("offsets que van hacia atrás");
            prev = o;
            out[j] = static_cast<std::size_t>(o);
        }
        return out;
    }

    static Value scalar(Prim p, const std::uint8_t* at) {
        switch (p) {
            case Prim::Bool: return Value::boolean(*at != 0);
            case Prim::I8: return Value::integer(get<std::int8_t>(at));
            case Prim::U8: return Value::uinteger(*at);
            case Prim::I16: return Value::integer(get<std::int16_t>(at));
            case Prim::U16: return Value::uinteger(get<std::uint16_t>(at));
            case Prim::I32: return Value::integer(get<std::int32_t>(at));
            case Prim::U32: return Value::uinteger(get<std::uint32_t>(at));
            case Prim::I64:
            case Prim::Time:
            case Prim::Duration: return Value::integer(get<std::int64_t>(at));
            case Prim::U64: return Value::uinteger(get<std::uint64_t>(at));
            case Prim::F32: return Value::real(get<float>(at));
            case Prim::F64: return Value::real(get<double>(at));
        }
        return {};
    }

    /// Una columna de `len` valores del nodo ni.
    std::vector<Value> read(std::uint32_t ni, std::size_t len) {
        const Codec::Node& n = c.nodes_[ni];
        (void)buf();  // validez
        // `len` lo dice el mensaje: se reserva memoria solo después de revisar
        // que los buffers de verdad traen esos valores (si no, un mensaje
        // chico pedía GB).
        std::vector<Value> out;
        switch (n.kind) {
            case Codec::Node::Kind::Prim: {
                if (len > bt.body.size()) bad("una columna con un largo inválido");
                const ByteView d = buf(len * n.size);
                out.reserve(len);
                for (std::size_t j = 0; j < len; ++j) out.push_back(scalar(n.prim, d.data() + j * n.size));
                break;
            }
            case Codec::Node::Kind::Bool: {
                const ByteView bits = buf(align_up(len, 8) / 8);
                out.reserve(len);
                for (std::size_t j = 0; j < len; ++j) out.push_back(Value::boolean(((bits[j >> 3] >> (j & 7)) & 1) != 0));
                break;
            }
            case Codec::Node::Kind::Utf8:
            case Codec::Node::Kind::Binary: {
                const auto offs = offsets(len);
                const ByteView d = buf(offs[len]);
                out.reserve(len);
                for (std::size_t j = 0; j < len; ++j) {
                    const ByteView part = d.subspan(offs[j], offs[j + 1] - offs[j]);
                    if (n.kind == Codec::Node::Kind::Utf8 && !valid_utf8(part)) bad("un campo string no es UTF-8 válido");
                    out.push_back(Value::blob(part));
                }
                break;
            }
            case Codec::Node::Kind::List:
            case Codec::Node::Kind::Fsl: {
                std::vector<std::size_t> offs;
                if (n.kind == Codec::Node::Kind::List) {
                    offs = offsets(len);
                } else {
                    // len·n sin desbordes: no puede pasar del largo máximo de una columna
                    if (n.n != 0 && len > max_len() / n.n) bad("una columna con un largo inválido");
                    offs.resize(len + 1);
                    for (std::size_t j = 0; j <= len; ++j) offs[j] = j * n.n;
                }
                const auto& child = c.nodes_[n.children[0]];
                node(offs[len]);
                if (child.kind == Codec::Node::Kind::Prim) {
                    (void)buf();
                    const ByteView d = buf(offs[len] * child.size);
                    out.reserve(len);
                    for (std::size_t j = 0; j < len; ++j) {
                        out.push_back(Value::raw_bytes(d.subspan(offs[j] * child.size, (offs[j + 1] - offs[j]) * child.size)));
                    }
                } else {
                    auto vals = read(n.children[0], offs[len]);
                    out.reserve(len);
                    for (std::size_t j = 0; j < len; ++j) {
                        std::vector<Value> items(std::make_move_iterator(vals.begin() + static_cast<std::ptrdiff_t>(offs[j])),
                                                 std::make_move_iterator(vals.begin() + static_cast<std::ptrdiff_t>(offs[j + 1])));
                        out.push_back(Value::list(std::move(items)));
                    }
                }
                break;
            }
            case Codec::Node::Kind::Struct: {
                std::vector<std::vector<Value>> cols;
                for (auto ch : n.children) {
                    node(len);
                    cols.push_back(read(ch, len));
                }
                out.reserve(len);
                for (std::size_t j = 0; j < len; ++j) {
                    std::vector<Value> fields;
                    fields.reserve(cols.size());
                    for (auto& col : cols) fields.push_back(std::move(col[j]));
                    out.push_back(Value::structure(std::move(fields)));
                }
                break;
            }
            case Codec::Node::Kind::Tensor: {
                node(len);  // data: List<T>
                (void)buf();
                const auto offs = offsets(len);
                node(offs[len]);
                (void)buf();
                const ByteView raw = buf(offs[len] * n.size);
                node(len);  // shape
                (void)buf();
                node(len * n.ndim);
                (void)buf();
                const ByteView sh = buf(4 * len * n.ndim);
                out.reserve(len);
                for (std::size_t j = 0; j < len; ++j) {
                    Value v;
                    v.kind = Value::Kind::Tensor;
                    for (std::size_t k = 0; k < n.ndim; ++k) {
                        const auto d = get<std::int32_t>(sh.data() + 4 * (j * n.ndim + k));
                        if (d < 0) bad("un tensor con forma negativa");
                        v.shape.push_back(d);
                    }
                    // La cuenta exacta: con una multiplicación que da la vuelta, una forma
                    // de 65536⁴ pasaba por un tensor sin datos.
                    std::uint64_t want = 0;
                    if (!shape_elems(v.shape, n.size, want) || want != offs[j + 1] - offs[j]) {
                        bad("un tensor trae " + std::to_string(offs[j + 1] - offs[j]) +
                            " datos y su forma dice otra cosa");
                    }
                    Value data = Value::raw_bytes(raw.subspan(offs[j] * n.size, (offs[j + 1] - offs[j]) * n.size));
                    data.kind = Value::Kind::Tensor;
                    data.shape = std::move(v.shape);
                    out.push_back(std::move(data));
                }
                break;
            }
        }
        return out;
    }
};

std::vector<Value> Codec::decode_rows(ByteView payload) const {
    const Batch bt = read_metadata(payload);
    Reader r{*this, bt};
    const Node& root = nodes_[root_];
    if (static_cast<std::uint64_t>(bt.rows) > payload.size()) bad("número de filas inválido");
    const auto rows = static_cast<std::size_t>(bt.rows);
    std::vector<std::vector<Value>> cols;
    for (auto ch : root.children) {
        r.node(rows);
        cols.push_back(r.read(ch, rows));
    }
    if (r.i_node != bt.nodes.size() || r.i_buf != bt.bufs.size()) {
        bad("el RecordBatch tiene " + std::to_string(bt.nodes.size()) + " columnas/" + std::to_string(bt.bufs.size()) +
            " buffers y el esquema '" + layout_->name() + "' usa " + std::to_string(r.i_node) + "/" +
            std::to_string(r.i_buf));
    }
    std::vector<Value> out;
    for (std::size_t j = 0; j < rows; ++j) {
        std::vector<Value> fields;
        for (auto& col : cols) fields.push_back(std::move(col[j]));
        out.push_back(Value::structure(std::move(fields)));
    }
    return out;
}

Value Codec::decode(ByteView payload) const {
    auto rows = decode_rows(payload);
    if (rows.size() != 1) {
        bad("se esperaba 1 fila de '" + layout_->name() + "' y llegaron " + std::to_string(rows.size()));
    }
    return std::move(rows[0]);
}

}  // namespace agmen::arrow
