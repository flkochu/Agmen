// agmen::schema::Layout (RFC 0007): el esquema leído en tiempo de ejecución da
// el mismo texto canónico, schema_id y tamaño que los vectores compartidos
// (tests/vectors/v1.txt), que también revisan Python y JavaScript.
#include <agmen/layout.hpp>
#include <agmen/wire.hpp>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#ifndef AGMEN_VECTORS_FILE
#error "CMake debe definir AGMEN_VECTORS_FILE"
#endif

namespace {

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

void test_vectors() {
    std::ifstream in(AGMEN_VECTORS_FILE);
    std::string line;
    int n = 0;
    while (std::getline(in, line)) {
        if (line.rfind("schema\t", 0) != 0) continue;
        const auto c = split_tabs(line);
        const agmen::schema::Layout l(c[1]);
        CHECK(l.text() == c[2]);
        CHECK(l.schema_id() == std::strtoull(c[3].c_str(), nullptr, 16));
        CHECK(l.size() == std::stoul(c[4]));
        ++n;
    }
    CHECK(n >= 8);
    std::printf("  %d esquemas de los vectores\n", n);
}

void test_tree() {
    const agmen::schema::Layout l("Pose{Vec3{f64 x;f64 y;f64 z} p;f32 yaw;u8[2][3] m}");
    const auto& root = l.type(l.root());
    CHECK(root.fields.size() == 3);
    CHECK(root.fields[0].offset == 0);
    CHECK(root.fields[1].offset == 24);
    CHECK(root.fields[2].offset == 28);
    const auto& m = l.type(root.fields[2].type);
    CHECK(m.kind == agmen::schema::Type::Kind::Array && m.count == 2);
    CHECK(l.type(m.elem).count == 3);
    CHECK(l.canonical(root.fields[2].type) == "u8[2][3]");
    CHECK(l.size() == 40);
    CHECK(l.name() == "Pose");
}

bool fails(const char* text, const char* contains) {
    try {
        const agmen::schema::Layout l(text);
        (void)l;
    } catch (const agmen::schema::SchemaError& e) {
        return std::string(e.what()).find(contains) != std::string::npos;
    }
    return false;
}

void test_errors() {
    CHECK(fails("A{float x}", "tipo desconocido 'float'"));
    CHECK(fails("A{f32 x;f32 x}", "repetido"));
    CHECK(fails("A{}", "no tiene campos"));
    CHECK(fails("A{f32[0] x}", "tamaño de arreglo"));
    CHECK(fails("A{f32 x} extra", "sobra texto"));
    CHECK(fails("agmen/2 A{f32 x}", "no soportada"));
}

std::string nested(int levels) {
    std::string s;
    for (int k = 0; k < levels; ++k) s += "A{";
    s += "u8 x";
    for (int k = 1; k < levels; ++k) s += "} a";
    return s + "}";
}

void structs_anidados_no_tumban() {
    // Antes: el lector era recursivo sin límite y ~3000 structs anidados (15 KB
    // de texto, lo que manda cualquiera en un anuncio) desbordaban la pila.
    CHECK(fails(nested(100000).c_str(), "demasiados niveles"));
    CHECK(fails(nested(33).c_str(), "demasiados niveles"));
    CHECK(agmen::schema::Layout(nested(32)).size() == 1);
    std::string many = "A{u8";
    for (int k = 0; k < 100000; ++k) many += "[]";
    CHECK(fails((many + " x}").c_str(), "demasiados niveles"));  // T[][][]...: también cuenta
}

void tamanos_sin_desbordes() {
    // Antes: 10^24 bytes daban la vuelta en size_t; el tamaño y los offsets
    // salían mal y decodificar leía fuera del payload.
    CHECK(fails("A{u8[1000000][1000000][1000000][1000000] x;u8 y}", "2 GiB"));
    CHECK(fails("A{string s;u8[1048576][1048576][1048576][16] x}", "2 GiB"));  // también dentro de uno variable
    CHECK(fails("A{u8[1048576][1500] a;u8[1048576][1500] b}", "2 GiB"));       // la suma de los campos
    CHECK(fails("A{tensor<f32,18446744073709551617> t}", "dimensiones"));
    const agmen::schema::Layout big("A{u8[1048576][2047] x}");
    CHECK(big.size() == 2146435072);
    // variable: los campos fijos no se suman (va en arrow)
    CHECK(!agmen::schema::Layout("A{u8[1048576][1500] a;u8[1048576][1500] b;string s}").fixed());
}

}  // namespace

int main() {
    std::puts("test_layout");
    test_vectors();
    test_tree();
    test_errors();
    structs_anidados_no_tumban();
    tamanos_sin_desbordes();
    if (g_failures != 0) {
        std::fprintf(stderr, "test_layout: %d fallas\n", g_failures);
        return 1;
    }
    std::puts("test_layout: todo bien");
    return 0;
}
