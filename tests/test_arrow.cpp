// La codificación arrow en C++ (RFC 0008) contra los vectores compartidos
// (tests/vectors/v1.txt), que también revisan Python (con pyarrow como juez)
// y JavaScript.
#include <agmen/arrow.hpp>
#include <agmen/layout.hpp>
#include <agmen/wire.hpp>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#ifndef AGMEN_VECTORS_FILE
#error "CMake debe definir AGMEN_VECTORS_FILE"
#endif

namespace {

using agmen::arrow::Codec;
using agmen::arrow::Value;
using agmen::wire::Bytes;
using agmen::wire::ByteView;
using agmen::wire::WireError;

int g_failures = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

std::vector<std::string> split_tabs(const std::string& line) {
    std::vector<std::string> out;
    std::stringstream ss(line);
    std::string part;
    while (std::getline(ss, part, '\t')) out.push_back(part);
    return out;
}

Bytes from_hex(const std::string& h) {
    Bytes out;
    for (std::size_t i = 0; i + 1 < h.size(); i += 2) out.push_back(static_cast<std::uint8_t>(std::stoul(h.substr(i, 2), nullptr, 16)));
    return out;
}

struct Row {
    std::string input, canon, payload_hex;
    std::uint64_t id = 0;
};

std::vector<Row> arrow_rows() {
    std::ifstream in(AGMEN_VECTORS_FILE);
    std::string line;
    std::vector<Row> out;
    while (std::getline(in, line)) {
        if (line.rfind("arrow\t", 0) != 0) continue;
        const auto c = split_tabs(line);
        out.push_back({c[1], c[2], c[5], std::strtoull(c[3].c_str(), nullptr, 16)});
    }
    return out;
}

void test_vectors() {
    int n = 0;
    for (const auto& r : arrow_rows()) {
        const agmen::schema::Layout l(r.input);
        CHECK(l.text() == r.canon);
        CHECK(l.schema_id() == r.id);
        CHECK(!l.fixed());
        const Codec codec(l);
        const Bytes payload = from_hex(r.payload_hex);
        const Value v = codec.decode(payload);
        // lo decodificado se vuelve a codificar con los mismos bytes que Python
        const Bytes again = codec.encode(v);
        if (again != payload) std::fprintf(stderr, "  distinto: %s\n", r.input.c_str());
        CHECK(again == payload);
        ++n;
    }
    CHECK(n >= 7);
    std::printf("  %d vectores arrow\n", n);
}

void test_values() {
    {
        const agmen::schema::Layout l("Texto{string s;u8 n}");
        const Codec c(l);
        const Bytes p = c.encode(Value::structure({Value::text("hola, ñandú"), Value::uinteger(7)}));
        const auto rows = arrow_rows();
        CHECK(p == from_hex(rows[0].payload_hex));
        const Value v = c.decode(p);
        CHECK(v.items[0].text() == "hola, ñandú");
        CHECK(v.items[1].as<int>() == 7);
    }
    {
        const agmen::schema::Layout l("Imagen{time timestamp;string frame_id;u32 width;u32 height;string encoding;u32 step;bytes data}");
        const Codec c(l);
        const std::vector<std::uint8_t> px{255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255};
        const Bytes p = c.encode(Value::structure({Value::integer(1700000000123456789), Value::text("camara"),
                                                   Value::uinteger(2), Value::uinteger(2), Value::text("rgb8"),
                                                   Value::uinteger(6), Value::blob(px)}));
        CHECK(p == from_hex(arrow_rows()[5].payload_hex));
        const Value v = c.decode(p);
        CHECK(v.items[6].bytes().size() == 12);
        CHECK(v.items[6].bytes()[4] == 255);
        // sin copiar: los bytes apuntan dentro del payload
        CHECK(v.items[6].bytes().data() >= p.data() && v.items[6].bytes().data() < p.data() + p.size());
        CHECK(static_cast<std::size_t>(v.items[6].bytes().data() - p.data()) % 8 == 0);
    }
    {
        const agmen::schema::Layout l("Tensor{tensor<f32,2> m;tensor<u8,1> v}");
        const Codec c(l);
        const std::vector<float> m{1, 2, 3, 4, 5, 6};
        const Bytes p = c.encode(Value::structure({Value::tensor<float>({2, 3}, m),
                                                   Value::tensor<std::uint8_t>({0}, std::span<const std::uint8_t>{})}));
        CHECK(p == from_hex(arrow_rows()[4].payload_hex));
        const Value v = c.decode(p);
        CHECK((v.items[0].shape == std::vector<std::int32_t>{2, 3}));
        CHECK(v.items[0].numbers<float>() == m);
    }
    {
        const agmen::schema::Layout l("Lista{f32[] x;i64[] y;bool[] z}");
        const Codec c(l);
        const std::vector<float> x{1.5F, -2.0F, 0.25F};
        const Bytes p = c.encode(Value::structure(
            {Value::raw<float>(x), Value::list({}),
             Value::list({Value::boolean(true), Value::boolean(false), Value::boolean(true), Value::boolean(true)})}));
        CHECK(p == from_hex(arrow_rows()[3].payload_hex));
        const Value v = c.decode(p);
        CHECK(v.items[0].numbers<float>() == x);
        CHECK(v.items[2].items.size() == 4 && v.items[2].items[3].b);
    }
}

template <class F>
bool schema_error(F&& f, const char* contains) {
    try {
        f();
    } catch (const agmen::schema::SchemaError& e) {
        if (std::string(e.what()).find(contains) != std::string::npos) return true;
        std::fprintf(stderr, "  mensaje: %s\n", e.what());
        return false;
    }
    return false;
}

void test_errors() {
    const agmen::schema::Layout l("A{string s;bytes b;f32[3] f;u8[] u;tensor<f32,2> t}");
    const Codec c(l);
    const std::vector<float> f3{1, 2, 3};
    const std::vector<float> f2{1, 2};
    const std::vector<float> t1{1};
    const auto make = [&](Value s, Value f, Value u, Value t) {
        return Value::structure({std::move(s), Value::blob({}), std::move(f), std::move(u), std::move(t)});
    };
    (void)c.encode(make(Value::text("x"), Value::raw<float>(f3), Value::list({Value::uinteger(1)}),
                        Value::tensor<float>({1, 1}, t1)));
    CHECK(schema_error([&] { (void)c.encode(make(Value::uinteger(1), Value::raw<float>(f3), Value::list({}), Value::tensor<float>({1, 1}, t1))); },
                       "debe ser texto"));
    CHECK(schema_error([&] { (void)c.encode(make(Value::text("x"), Value::raw<float>(f2), Value::list({}), Value::tensor<float>({1, 1}, t1))); },
                       "'f' debe tener 3 elementos, tiene 2"));
    CHECK(schema_error([&] { (void)c.encode(make(Value::text("x"), Value::raw<float>(f3), Value::list({Value::uinteger(300)}), Value::tensor<float>({1, 1}, t1))); },
                       "no cabe en u8 (de 0 a 255)"));
    CHECK(schema_error([&] { (void)c.encode(make(Value::text("x"), Value::raw<float>(f3), Value::list({}), Value::tensor<float>({2}, t1))); },
                       "llegó con 1 dimensiones"));
    CHECK(schema_error([&] { (void)c.encode(make(Value::text("x"), Value::raw<float>(f3), Value::list({}), Value::tensor<float>({2, 2}, t1))); },
                       "4 elementos"));
    const char bad_utf8[] = "\xff\xfe";
    CHECK(schema_error([&] { (void)c.encode(make(Value::blob({reinterpret_cast<const std::uint8_t*>(bad_utf8), 2}), Value::raw<float>(f3), Value::list({}), Value::tensor<float>({1, 1}, t1))); },
                       "UTF-8"));
    CHECK(schema_error([] { (void)Codec(agmen::schema::Layout("A{f32 x}")); }, "tamaño fijo"));

    for (const auto* text : {"A{str s}", "A{optional f32 x}", "A{tensor<bool,2> t}", "A{tensor<f32,0> t}", "A{tensor f32 t}"}) {
        bool threw = false;
        try {
            (void)agmen::schema::Layout(text);
        } catch (const agmen::schema::SchemaError&) {
            threw = true;
        }
        CHECK(threw);
    }
    CHECK(agmen::schema::Layout("A{ f32 [ ] [3] p ; tensor < f32 , 2 > m; string[] s; bytes b }").text() ==
          "agmen/1 A{f32[][3] p;tensor<f32,2> m;string[] s;bytes b}");
}

void test_broken_payloads() {
    const agmen::schema::Layout l("Mixto{string[] s;f32[] x;tensor<u8,2> t;Vec{f32 a;string b}[] v}");
    const Codec c(l);
    const std::vector<std::uint8_t> t{1, 2};
    const Bytes good = c.encode(Value::structure(
        {Value::list({Value::text("a"), Value::text("bb")}), Value::list({Value::real(1)}),
         Value::tensor<std::uint8_t>({1, 2}, t),
         Value::list({Value::structure({Value::real(1), Value::text("z")})})}));
    (void)c.decode(good);
    const auto throws = [&](const Bytes& b) {
        try {
            (void)c.decode(b);
        } catch (const WireError&) {
            return true;
        }
        return false;
    };
    CHECK(throws({}));
    CHECK(throws(Bytes(16, 0)));
    CHECK(throws(Bytes(good.begin(), good.begin() + 40)));
    CHECK(throws(Bytes(good.begin(), good.end() - 8)));
    // basura: nunca otra excepción ni un acceso fuera del buffer (el CI corre esto con ASan)
    std::mt19937 rng(7);
    int rejected = 0;
    for (int k = 0; k < 20000; ++k) {
        Bytes b = good;
        const int changes = 1 + static_cast<int>(rng() % 6);
        for (int j = 0; j < changes; ++j) b[rng() % b.size()] = static_cast<std::uint8_t>(rng());
        try {
            (void)c.decode(b);
        } catch (const WireError&) {
            ++rejected;
        }
    }
    CHECK(rejected > 1000);
}

void test_envelope() {
    std::ifstream in(AGMEN_VECTORS_FILE);
    std::string line;
    int n = 0;
    while (std::getline(in, line)) {
        if (line.rfind("message\t", 0) != 0 || line.find("Imagen") == std::string::npos) continue;
        const auto c = split_tabs(line);
        const Bytes packet = from_hex(c[8]);
        const auto [env, payload] = agmen::wire::Envelope::read_from(packet);
        CHECK(env.encoding == agmen::wire::Encoding::Arrow);
        const agmen::schema::Layout l(c[1]);
        const Codec codec(l);
        CHECK(codec.decode(payload).items[1].text() == "camara");
        ++n;
    }
    CHECK(n == 1);
}

}  // namespace

int main() {
    std::printf("test_arrow\n");
    test_vectors();
    test_values();
    test_errors();
    test_broken_payloads();
    test_envelope();
    if (g_failures != 0) {
        std::printf("%d fallas\n", g_failures);
        return 1;
    }
    std::printf("OK\n");
    return 0;
}
