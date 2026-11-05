// La codificación arrow en C++ (RFC 0008) contra los vectores compartidos
// (tests/vectors/v1.txt), que también revisan Python (con pyarrow como juez)
// y JavaScript.
#include <agmen/arrow.hpp>
#include <agmen/layout.hpp>
#include <agmen/wire.hpp>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <new>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#ifndef AGMEN_VECTORS_FILE
#error "CMake debe definir AGMEN_VECTORS_FILE"
#endif

// La reserva de memoria más grande desde la última vez que se puso en 0 (ver
// memoria_acotada_con_datos_que_faltan). Este programa es de un solo hilo.
static std::size_t g_biggest_alloc = 0;
void* operator new(std::size_t n) {
    if (n > g_biggest_alloc) g_biggest_alloc = n;
    if (void* p = std::malloc(n == 0 ? 1 : n)) return p;
    throw std::bad_alloc();
}
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }

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

std::int64_t get_le_i64(const Bytes& b, std::size_t at) {
    std::uint64_t v = 0;
    for (std::size_t k = 0; k < 8; ++k) v |= static_cast<std::uint64_t>(b[at + k]) << (8 * k);
    return static_cast<std::int64_t>(v);
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

bool throws_wire(const Codec& c, const Bytes& b, const char* contains) {
    try {
        (void)c.decode(b);
    } catch (const WireError& e) {
        if (std::string(e.what()).find(contains) != std::string::npos) return true;
        std::fprintf(stderr, "  mensaje: %s\n", e.what());
    }
    return false;
}

// Reemplaza la única aparición de `from` en `b` por `to` (mismo tamaño).
bool patch(Bytes& b, const Bytes& from, const Bytes& to) {
    std::size_t found = 0;
    std::size_t at = 0;
    for (std::size_t k = 0; k + from.size() <= b.size(); ++k) {
        if (std::memcmp(b.data() + k, from.data(), from.size()) == 0) {
            ++found;
            at = k;
        }
    }
    if (found != 1) return false;
    std::memcpy(b.data() + at, to.data(), to.size());
    return true;
}

Bytes i32s(std::initializer_list<std::int32_t> v) {
    Bytes out;
    for (const auto x : v) {
        for (int k = 0; k < 4; ++k) out.push_back(static_cast<std::uint8_t>(static_cast<std::uint32_t>(x) >> (8 * k)));
    }
    return out;
}

void forma_de_tensor_que_da_la_vuelta() {
    // Antes: los elementos de la forma se multiplicaban en size_t; 65536⁴ = 2^64
    // daba 0 y un tensor "de 2^64 elementos" pasaba sin datos (al leer y al escribir).
    const agmen::schema::Layout l("A{tensor<u8,4> t}");
    const Codec c(l);
    const std::span<const std::uint8_t> none{};
    Bytes b = c.encode(Value::structure({Value::tensor<std::uint8_t>({0, 3, 5, 7}, none)}));
    CHECK(c.decode(b).items[0].shape.size() == 4);
    CHECK(patch(b, i32s({0, 3, 5, 7}), i32s({65536, 65536, 65536, 65536})));
    CHECK(throws_wire(c, b, "su forma dice otra cosa"));
    CHECK(schema_error([&] { (void)c.encode(Value::structure({Value::tensor<std::uint8_t>({65536, 65536, 65536, 65536}, none)})); },
                       "se pueden contar"));
    CHECK(schema_error([&] { (void)c.encode(Value::structure({Value::tensor<std::uint8_t>({65536, 65536, 65535, 1}, none)})); },
                       "elementos según su forma y trae 0 datos"));
    // Una dimensión 0 da 0 elementos; las demás, como en NumPy, deben caber en
    // 63 bits (contando el 0 como 1). Lo encontró atheris: NumPy lanzaba
    // ValueError con (469762048, 402653184, 0, 67108864).
    Bytes zero = c.encode(Value::structure({Value::tensor<std::uint8_t>({0, 65536, 65536, 65536}, none)}));
    CHECK((c.decode(zero).items[0].shape == std::vector<std::int32_t>{0, 65536, 65536, 65536}));
    CHECK(schema_error([&] { (void)c.encode(Value::structure({Value::tensor<std::uint8_t>({469762048, 402653184, 0, 67108864}, none)})); },
                       "se pueden contar"));
    CHECK(patch(zero, i32s({0, 65536, 65536, 65536}), i32s({469762048, 402653184, 0, 67108864})));
    CHECK(throws_wire(c, zero, "su forma dice otra cosa"));
}

void flotante_fuera_de_rango_en_entero_de_64_bits() {
    // Antes: static_cast<int64_t>(1e30) es comportamiento indefinido (UBSan).
    const agmen::schema::Layout l("A{string s;i64 a;u64 b;time t}");
    const Codec c(l);
    const auto row = [](Value a, Value b, Value t) {
        return Value::structure({Value::text("x"), std::move(a), std::move(b), std::move(t)});
    };
    const Value ok = c.decode(c.encode(row(Value::real(-9.2e18), Value::real(1.8e19), Value::real(1e18))));
    // Enteros literales: MSVC avisa C4056 si se convierte 1.8e19 en compilación (pasa de i64).
    CHECK(ok.items[1].i == -9'200'000'000'000'000'000LL && ok.items[2].u == 18'000'000'000'000'000'000ULL);
    const double nan = std::numeric_limits<double>::quiet_NaN();
    for (const double bad : {1e30, -1e30, 0x1p63, nan, 1.5}) {
        CHECK(schema_error([&] { (void)c.encode(row(Value::real(bad), Value::uinteger(0), Value::integer(0))); }, "i64"));
        CHECK(schema_error([&] { (void)c.encode(row(Value::integer(0), Value::uinteger(0), Value::real(bad))); }, "time"));
    }
    for (const double bad : {-1.0, 0x1p64, 1e30, nan, 0.5}) {
        CHECK(schema_error([&] { (void)c.encode(row(Value::integer(0), Value::real(bad), Value::integer(0))); }, "u64"));
    }
}

void memoria_acotada_con_datos_que_faltan() {
    // Antes: al leer una columna se reservaba lugar para sus valores ANTES de
    // revisar que el mensaje trajera los datos: 131072 bool sin sus bits (un
    // mensaje de 33 KB) pedían ~15 MB, y uno de 64 MiB, decenas de GB.
    const agmen::schema::Layout l("A{bool[131072] b;bytes pad}");
    const Codec c(l);
    std::vector<Value> bits(131072, Value::boolean(true));
    const std::vector<std::uint8_t> pad(16384, 7);
    Bytes b = c.encode(Value::structure({Value::list(std::move(bits)), Value::blob(pad)}));
    // Metadatos de Agmen: 3 FieldNode y luego los Buffer (offset, largo); el
    // tercero son los bits. Se dice que mide 0.
    const std::size_t bits_len_at = 8 + (80 + 16 * 3) + 16 + 16 * 2;
    CHECK(get_le_i64(b, bits_len_at) == 16384);
    for (std::size_t k = 0; k < 8; ++k) b[bits_len_at + k] = 0;
    g_biggest_alloc = 0;
    CHECK(throws_wire(c, b, "se necesitan 16384"));
    CHECK(g_biggest_alloc < (std::size_t{1} << 20));
    if (g_biggest_alloc >= (std::size_t{1} << 20)) std::fprintf(stderr, "  pidió %zu bytes\n", g_biggest_alloc);
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
    forma_de_tensor_que_da_la_vuelta();
    flotante_fuera_de_rango_en_entero_de_64_bits();
    memoria_acotada_con_datos_que_faltan();
    test_envelope();
    if (g_failures != 0) {
        std::printf("%d fallas\n", g_failures);
        return 1;
    }
    std::printf("OK\n");
    return 0;
}
