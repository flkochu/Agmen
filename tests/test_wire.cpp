// Pruebas del formato v1 (RFC 0001). Se corren con `ctest`.
//
// Sin frameworks externos a propósito: un mini-arnés de 20 líneas es
// suficiente y no agrega dependencias. Lo más importante: comparar byte por
// byte contra tests/vectors/v1.txt, los MISMOS vectores que revisa Python.
// Si alguna falla, rompiste la compatibilidad con Python u otros lenguajes.

#include <agmen/hubproto.hpp>
#include <agmen/wire.hpp>

#include <cstdio>
#include <fstream>
#include <functional>
#include <map>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

#ifndef AGMEN_VECTORS_FILE
#error "CMake debe definir AGMEN_VECTORS_FILE"
#endif

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                      \
    do {                                                                                 \
        if (!(cond)) {                                                                   \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            ++g_failures;                                                                \
        }                                                                                \
    } while (0)

template <class F>
void expect_error(F&& f, agmen::wire::WireError::Kind kind, std::string_view must_contain = {}) {
    try {
        f();
        std::fprintf(stderr, "  FALLA: se esperaba WireError y no se lanzó\n");
        ++g_failures;
    } catch (const agmen::wire::WireError& e) {
        CHECK(e.kind() == kind);
        if (std::string_view(e.what()).find(must_contain) == std::string_view::npos) {
            std::fprintf(stderr, "  FALLA: el error \"%s\" no contiene \"%.*s\"\n", e.what(),
                         static_cast<int>(must_contain.size()), must_contain.data());
            ++g_failures;
        }
    }
}

using namespace agmen::wire;

// --- Tipos de los vectores ---------------------------------------------------

struct MotorCommand {
    static constexpr std::string_view kName = "MotorCommand";
    static constexpr std::string_view kSchema = "MotorCommand{f32 left;f32 right}";
    float left = 0, right = 0;
    void encode_payload(Bytes& out) const {
        put_f32(out, left);
        put_f32(out, right);
    }
    static MotorCommand decode_payload(ByteView p) { return {get_f32(p, 0), get_f32(p, 4)}; }
};

// Con relleno: u8 en 0, 7 bytes de ceros y luego 7 f64 alineados a 8.
struct JointCommand7 {
    static constexpr std::string_view kName = "JointCommand7";
    static constexpr std::string_view kSchema = "JointCommand7{u8 mode;f64[7] position}";
    std::uint8_t mode = 0;
    std::array<double, 7> position{};
    void encode_payload(Bytes& out) const {
        out.push_back(mode);
        put_padding(out, 7);
        for (double v : position) put_f64(out, v);
    }
    static JointCommand7 decode_payload(ByteView p) {
        JointCommand7 j{p[0], {}};
        for (std::size_t k = 0; k < 7; ++k) j.position[k] = get_f64(p, 8 + 8 * k);
        return j;
    }
};

static_assert(Message<MotorCommand> && Message<JointCommand7>);

// --- Esquemas: se revisan en COMPILACIÓN ---------------------------------------

static_assert(fnv1a64("") == 0xcbf29ce484222325ULL);
static_assert(fnv1a64("a") == 0xaf63dc4c8601ec8cULL);
static_assert(schema::check("MotorCommand{f32 left;f32 right}").size == 8);
static_assert(schema::check("JointCommand7{u8 mode;f64[7] position}").size == 64);
static_assert(schema::check("Pose{Vec3{f64 x;f64 y;f64 z} p;f32 yaw}").size == 32);
static_assert(schema::check("Flags{bool a;u16 b;u8 c}").size == 6);
static_assert(schema::check("Matriz{f32[2][3] m}").size == 24);
static_assert(schema::check("Tiempos{time t;duration d;i8 x}").size == 24);
// No canónicos o inválidos: el compilador los rechaza con AGMEN_CHECK_SCHEMA.
static_assert(!schema::check("MotorCommand{f32 left;f32 right;}").ok);   // ';' final
static_assert(!schema::check("MotorCommand{f32  left;f32 right}").ok);   // doble espacio
static_assert(!schema::check("MotorCommand { f32 left;f32 right}").ok);  // espacio
static_assert(!schema::check("Motor{float left}").ok);                   // tipo de C
static_assert(!schema::check("A{f32 x;f32 x}").ok);                      // campo repetido
static_assert(!schema::check("A{f32[0] x}").ok);
static_assert(!schema::check("A{f32[07] x}").ok);
static_assert(!schema::check("A{}").ok);
static_assert(!schema::check("A{string s}").ok);                         // variable: nivel 7
static_assert(!schema::check("agmen/1 A{f32 x}").ok);                    // sin prefijo en kSchema

// --- Vectores compartidos con Python -------------------------------------------

std::vector<std::vector<std::string>> load_vectors(std::string_view kind) {
    std::ifstream in(AGMEN_VECTORS_FILE);
    std::vector<std::vector<std::string>> rows;
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == '#') continue;
        std::vector<std::string> cols;
        std::stringstream ss(line);
        std::string col;
        while (std::getline(ss, col, '\t')) cols.push_back(col);
        if (line.back() == '\t') cols.emplace_back();
        if (!cols.empty() && cols[0] == kind) rows.push_back(cols);
    }
    return rows;
}

std::string hex(ByteView b) {
    static constexpr char d[] = "0123456789abcdef";
    std::string s;
    for (auto x : b) {
        s += d[x >> 4];
        s += d[x & 15];
    }
    return s;
}

std::uint64_t u64(const std::string& s) { return std::stoull(s, nullptr, 0); }
std::int64_t i64(const std::string& s) { return std::stoll(s); }

void vectores_de_esquemas() {
    const auto rows = load_vectors("schema");
    CHECK(rows.size() >= 8);
    for (const auto& r : rows) {
        // r = schema, entrada, canónico, schema_id, tamaño, valores, payload
        const std::string& canon = r[2];
        CHECK(canon.starts_with(kSchemaPrefix));
        const auto body = std::string_view(canon).substr(kSchemaPrefix.size());
        const auto c = schema::check(body);
        if (!c.ok || c.size != std::stoul(r[4]) || fnv1a64(canon) != u64(r[3])) {
            std::fprintf(stderr, "  FALLA: vector de esquema '%s'\n", canon.c_str());
            ++g_failures;
        }
    }
}

void vectores_de_mensajes() {
    const auto rows = load_vectors("message");
    CHECK(rows.size() == 3);
    const auto envelope_of = [](const std::vector<std::string>& r) {
        // r = message, canónico, topic, source_id, seq, t_data, t_pub, payload, paquete
        return Envelope{.topic_id = topic_id(r[2]), .source_id = u64(r[3]), .seq = u64(r[4]),
                        .t_data = i64(r[5]), .t_pub = i64(r[6])};
    };
    for (const auto& r : rows) {
        Bytes got;
        if (r[1] == schema_text<MotorCommand>()) {
            got = encode(MotorCommand{0.5f, -0.25f}, envelope_of(r));
            const auto d = decode<MotorCommand>(got);
            CHECK(d.message.left == 0.5f && d.message.right == -0.25f);
        } else if (r[1] == schema_text<ImuSample>()) {
            got = encode(ImuSample{{0.1f, -9.81f, 0.0f}, {0.01f, 0.02f, -0.03f}}, envelope_of(r));
            CHECK(decode<ImuSample>(got).message.accel[1] == -9.81f);
        } else if (r[1] == schema_text<JointCommand7>()) {
            got = encode(JointCommand7{3, {1, 2, 3, 4, 5, 6, 7}}, envelope_of(r));
            const auto d = decode<JointCommand7>(got);
            CHECK(d.message.mode == 3 && d.message.position[6] == 7.0);
        }
        if (hex(got) != r[8]) {
            std::fprintf(stderr, "  FALLA: bytes de '%s'\n    C++:    %s\n    Python: %s\n",
                         r[1].c_str(), hex(got).c_str(), r[8].c_str());
            ++g_failures;
        }
        // Y el sobre se lee de vuelta igual.
        const auto [env, payload] = Envelope::read_from(got);
        CHECK(env.seq == u64(r[4]) && env.t_data == i64(r[5]) && env.t_pub == i64(r[6]));
        CHECK(env.source_id == u64(r[3]) && env.topic_id == topic_id(r[2]));
        CHECK(hex(payload) == r[7]);
    }
}

void vectores_de_anuncios() {
    const auto rows = load_vectors("announcement");
    CHECK(rows.size() == 2);
    for (const auto& r : rows) {
        // r = announcement, canónico, topic, source_id, seq, t, paquete
        const auto got = encode_announcement(
            r[1], r[2], Envelope{.source_id = u64(r[3]), .seq = u64(r[4]), .t_pub = i64(r[5])});
        CHECK(hex(got) == r[6]);
        const auto [env, payload] = Envelope::read_from(got);
        CHECK(env.encoding == Encoding::Announcement);
        const auto a = read_announcement(env, payload);
        CHECK(a.topic == r[2] && a.schema == r[1]);
    }
}

void vectores_fnv() {
    for (const auto& r : load_vectors("fnv1a64")) CHECK(fnv1a64(r[1]) == u64(r[2]));
}

// --- Validación (RFC 0001 §8) ----------------------------------------------------

ImuSample sample() { return ImuSample{{0.1f, -9.81f, 0.0f}, {0.01f, 0.02f, -0.03f}}; }

void layout_del_sobre_es_estable() {
    const auto b = encode(sample(), Envelope{.seq = 1, .t_data = 2, .t_pub = 3});
    CHECK(b.size() == kEnvelopeLen + 24);
    CHECK(b[0] == 'A' && b[1] == 'G' && b[2] == 'M' && b[3] == 1);
    CHECK(get_le<std::uint16_t>(b, 4) == 64);
    CHECK(b[6] == 1);  // compact
    CHECK(get_le<std::uint64_t>(b, 8) == schema_id_v<ImuSample>);
    CHECK(get_le<std::uint64_t>(b, 32) == 1);
    CHECK(get_le<std::uint64_t>(b, 40) == 2);
    CHECK(get_le<std::uint64_t>(b, 48) == 3);
    CHECK(get_le<std::uint32_t>(b, 56) == 24);
    CHECK(b[61] == 0 && b[62] == 0 && b[63] == 0);
    // -9.81f en little-endian: 0xC11CF5C3 -> C3 F5 1C C1, en el byte 64 + 4
    CHECK(b[68] == 0xC3 && b[69] == 0xF5 && b[70] == 0x1C && b[71] == 0xC1);
}

void rechaza_lo_invalido() {
    const auto good = encode(sample(), 0, 0);
    auto b = good;
    b[0] = 'X';
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::BadMagic, "Agmen");
    b = good;
    b[3] = 0;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::UnsupportedVersion,
                 "Agmen 0.4");
    b = good;
    b[3] = 7;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::UnsupportedVersion);
    b = good;
    b[8] ^= 0xFF;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::TypeMismatch,
                 "TIPO INCOMPATIBLE");
    expect_error([&] { (void)decode<ImuSample>(ByteView(good).first(10)); },
                 WireError::Kind::TooShort);
    expect_error([&] { (void)decode<ImuSample>(ByteView(good).first(70)); },
                 WireError::Kind::PayloadLenMismatch);
    b = good;
    b[6] = 9;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::UnknownEncoding);
    b = good;
    b[6] = 2;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::Unsupported, "arrow");
    b = good;
    b[7] = 0x80;
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::UnknownEncoding, "flags");
    b = good;
    b[4] = 10;  // header_len < 64
    expect_error([&] { (void)decode<ImuSample>(b); }, WireError::Kind::BadHeader);
    expect_error([&] { (void)decode<MotorCommand>(good); }, WireError::Kind::TypeMismatch,
                 "MotorCommand");
    const auto ann = encode_announcement(schema_text<ImuSample>(), "/imu", Envelope{});
    expect_error([&] { (void)decode<ImuSample>(ann); }, WireError::Kind::NotData);
}

void sobre_mas_largo_se_salta() {
    // Un sobre de una versión futura con header_len = 72: los 8 bytes extra
    // se saltan y el payload se lee igual (así se agregan campos sin romper).
    auto b = encode(sample(), 5, 9);
    b.insert(b.begin() + 64, 8, std::uint8_t{0xEE});
    b[4] = 72;
    const auto d = decode<ImuSample>(b);
    CHECK(d.envelope.seq == 5 && d.message == sample());
}

void anuncio_alterado_se_rechaza() {
    auto b = encode_announcement(schema_text<MotorCommand>(), "/cmd", Envelope{});
    b.back() = 'X';  // cambiamos el texto: ya no coincide con su schema_id
    const auto [env, payload] = Envelope::read_from(b);
    expect_error([&] { (void)read_announcement(env, payload); }, WireError::Kind::BadAnnouncement);
}

// --- Protocolo del hub: los mismos casos que python/test_hub.py ---------------

void hub_protocolo() {
    using namespace agmen;
    CHECK(hub::quote("a b=c%") == "a%20b%3Dc%25");
    CHECK(hub::quote("/cmd,1.5:*-_") == "/cmd,1.5:*-_");
    CHECK(hub::quote("ñ") == "%C3%B1");
    const std::string text = "agmen/1 Pose{Vec3{f64 x;f64 y} p;f32[3] q}";
    CHECK(hub::unquote(hub::quote(text)) == text);
    const auto ln = hub::line("pub", {{"id", "ab"}, {"topic", "/imu"}, {"text", "X{f32 a}"}});
    CHECK(ln == "agmen-hub/1 pub id=ab topic=/imu text=X%7Bf32%20a%7D");  // igual que Python
    const auto parsed =
        hub::parse(ByteView(reinterpret_cast<const std::uint8_t*>(ln.data()), ln.size()));
    CHECK(parsed.size() == 1 && parsed[0].op == "pub" && parsed[0].get("text") == "X{f32 a}");
    const std::string two = "agmen-hub/1 subs id=1 to=127.0.0.1:5,127.0.0.1:6\nagmen-hub/1 ok id=2";
    const auto many =
        hub::parse(ByteView(reinterpret_cast<const std::uint8_t*>(two.data()), two.size()));
    CHECK(many.size() == 2 && many[0].get("to") == "127.0.0.1:5,127.0.0.1:6");
    for (const char* ok : {"/imu", "/arca9/articulaciones", "/cmd/teleop", "/A_1"}) {
        try {
            hub::check_topic(ok);
        } catch (const std::invalid_argument&) {
            std::fprintf(stderr, "  FALLA: '%s' debería ser válido\n", ok);
            ++g_failures;
        }
    }
    for (const char* bad : {"imu", "/", "/imu/", "//imu", "/i mu", "/imu-1", ""}) {
        bool threw = false;
        try {
            hub::check_topic(bad);
        } catch (const std::invalid_argument&) {
            threw = true;
        }
        if (!threw) {
            std::fprintf(stderr, "  FALLA: '%s' debería ser inválido\n", bad);
            ++g_failures;
        }
    }
    CHECK(hub::is_topic("/imu") && !hub::is_topic("127.0.0.1:7500"));
}

void source_id_aleatorio() {
    CHECK(random_source_id() != 0);
    CHECK(random_source_id() != random_source_id());
}

}  // namespace

int main() {
    const std::vector<std::pair<const char*, std::function<void()>>> tests = {
        {"vectores_de_esquemas", vectores_de_esquemas},
        {"vectores_de_mensajes", vectores_de_mensajes},
        {"vectores_de_anuncios", vectores_de_anuncios},
        {"vectores_fnv", vectores_fnv},
        {"layout_del_sobre_es_estable", layout_del_sobre_es_estable},
        {"rechaza_lo_invalido", rechaza_lo_invalido},
        {"sobre_mas_largo_se_salta", sobre_mas_largo_se_salta},
        {"anuncio_alterado_se_rechaza", anuncio_alterado_se_rechaza},
        {"source_id_aleatorio", source_id_aleatorio},
        {"hub_protocolo", hub_protocolo},
    };
    for (const auto& [name, fn] : tests) {
        const int before = g_failures;
        try {
            fn();
        } catch (const std::exception& e) {
            std::fprintf(stderr, "  FALLA: excepción %s\n", e.what());
            ++g_failures;
        }
        std::printf("%s %s\n", g_failures == before ? "ok   " : "FALLA", name);
    }
    std::printf("\n%zu pruebas, %d fallas\n", tests.size(), g_failures);
    return g_failures == 0 ? 0 : 1;
}
