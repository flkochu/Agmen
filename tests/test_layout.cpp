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

}  // namespace

int main() {
    std::puts("test_layout");
    test_vectors();
    test_tree();
    test_errors();
    if (g_failures != 0) {
        std::fprintf(stderr, "test_layout: %d fallas\n", g_failures);
        return 1;
    }
    std::puts("test_layout: todo bien");
    return 0;
}
