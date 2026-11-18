// Los nombres de las articulaciones declarados al hub desde C++ (RFC 0013 §3.4, D6 = d):
// TopicOptions::names en Publisher y Subscriber, y SubscribeOptions::names en Node.
//
// - La lista se revisa al crear, antes de abrir nada, con los mismos textos que Python
//   (python/agmen/hubproto.py check_joint_names y python/agmen/pubsub.py joint_names_for; la comparación
//   texto a texto con Python está en python/test_nombres.py, TestNucleoCpp).
// - Sin lista, la línea de registro es la de siempre, byte a byte; con lista, lleva `names=a,b` al final.
// - El rechazo del hub llega como el de siempre (WireError HubRejected, al publicar o recibir), con su
//   texto entero.
//
// Un hub de mentira: un socket UDP que lee las líneas de registro y, si se le pide, contesta `error`.
#include <agmen/agmen.hpp>
#include <agmen/joints.hpp>

#include <atomic>
#include <chrono>
#include <filesystem>
#include <map>
#include <set>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

// El 65507 de un datagrama está en tres lugares de C++ (hubproto.hpp, joints.hpp y src/net.cpp, que no se
// incluyen entre sí): los dos que se ven desde aquí no pueden divergir sin que esto deje de compilar.
static_assert(agmen::hub::kMaxDatagram == agmen::msgs::joints_detail::kMaxPayload + agmen::wire::kEnvelopeLen,
              "hub::kMaxDatagram (hubproto.hpp) y joints_detail::kMaxPayload + el sobre (joints.hpp) deben ser el "
              "mismo datagrama: 65507");

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::fprintf(stderr, "  FALLA %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

#define CHECK_EQ(a, b)                                                                                    \
    do {                                                                                                  \
        const std::string _a = (a), _b = (b);                                                             \
        if (_a != _b) {                                                                                   \
            std::fprintf(stderr, "  FALLA %s:%d:\n    %s\n    != %s\n", __FILE__, __LINE__, _a.c_str(), \
                         _b.c_str());                                                                     \
            ++g_failures;                                                                                 \
        }                                                                                                 \
    } while (0)

using namespace std::chrono_literals;
namespace hub = agmen::hub;
namespace net = agmen::net;
namespace wire = agmen::wire;
namespace m = agmen::msgs;
using wire::ByteView;

/// En Windows una variable de entorno mide como mucho 32 767 caracteres (con el '\0'): con un valor más largo,
/// _putenv_s no devuelve un error, sino que llama al manejador de "parámetro inválido" de la CRT, que en Release
/// termina el proceso sin decir nada (0xc0000409). Las pruebas no deben pasar de este tope, en ningún sistema:
/// así la misma prueba corre igual en Linux y en Windows.
constexpr std::size_t kMaxEnvValue = 32766;

void set_env(const char* name, const char* value) {
    const std::size_t len = std::strlen(value);
    if (len > kMaxEnvValue) {
        throw std::length_error("set_env(" + std::string(name) + "): el valor mide " + std::to_string(len) +
                                " caracteres y en Windows una variable de entorno mide como mucho " +
                                std::to_string(kMaxEnvValue) + ": usa un valor más corto en la prueba");
    }
#ifdef _WIN32
    const int rc = _putenv_s(name, value);
#else
    const int rc = setenv(name, value, 1);
#endif
    if (rc != 0) throw std::runtime_error("set_env(" + std::string(name) + ") falló (código " + std::to_string(rc) + ")");
}

/// Un nombre de nodo que, escrito como va en la línea del hub (hub::quote), mide `bytes` bytes, sin pasar del tope
/// de una variable de entorno de Windows: "nodo" y luego '%', que va como "%25" (3 bytes por carácter).
/// `bytes` - 4 debe ser múltiplo de 3.
std::string nodo_que_mide(std::size_t bytes) {
    const std::string nodo = "nodo" + std::string((bytes - 4) / 3, '%');
    if (hub::quote(nodo).size() != bytes || nodo.size() > kMaxEnvValue) {
        throw std::logic_error("nodo_que_mide(" + std::to_string(bytes) + "): no se puede armar ese nombre");
    }
    return nodo;
}

const std::vector<std::string> kPatas = {"FR_hip", "FR_thigh", "FR_calf", "FL_hip", "FL_thigh", "FL_calf",
                                         "RR_hip", "RR_thigh", "RR_calf", "RL_hip", "RL_thigh", "RL_calf"};
const std::string kPatasText =
    "FR_hip,FR_thigh,FR_calf,FL_hip,FL_thigh,FL_calf,RR_hip,RR_thigh,RR_calf,RL_hip,RL_thigh,RL_calf";

const std::string kHelp =
    "Usa letras (sin acentos), dígitos, '_', '-', '.' o '/', y empieza con letra, dígito o '_', como en el "
    "modelo (MuJoCo o URDF), ej. \"FR_hip\" o \"brazo/codo.1\"";

/// Un hub de mentira (sin hilos): lee las líneas de registro que le llegan y contesta lo que se le pida.
class FakeHub {
public:
    FakeHub() : sock_(net::UdpSocket::bind("127.0.0.1:0")) {
        addr = "127.0.0.1:" + std::to_string(sock_.local_port());
        set_env("AGMEN_HUB", addr.c_str());
    }

    /// La siguiente línea `op` que llegue (las demás se saltan; op "" = cualquiera); "" si no llega ninguna a tiempo.
    std::string next(const std::string& op, std::chrono::milliseconds tope = 3000ms) {
        std::vector<std::uint8_t> buf(65536);
        const auto end = std::chrono::steady_clock::now() + tope;
        while (std::chrono::steady_clock::now() < end) {
            const auto rx = sock_.recv_from(buf, 50ms);
            if (!rx) continue;
            std::string text(reinterpret_cast<const char*>(buf.data()), rx->size);
            if (text.rfind(std::string(hub::kPrefix) + " " + op + (op.empty() ? "" : " "), 0) == 0) {
                last_from = rx->from;
                return text;
            }
        }
        return {};
    }

    /// ¿Llegó alguna línea `op` en `tope`?
    bool any(const std::string& op, std::chrono::milliseconds tope = 300ms) { return !next(op, tope).empty(); }

    void reply(const std::string& line) {
        (void)sock_.send_to(ByteView(reinterpret_cast<const std::uint8_t*>(line.data()), line.size()),
                            net::Address::resolve(last_from));
    }

    std::string addr;
    std::string last_from;

private:
    net::UdpSocket sock_;
};

std::string field(const std::string& line, const std::string& key) {
    const auto lines = hub::parse(ByteView(reinterpret_cast<const std::uint8_t*>(line.data()), line.size()));
    return lines.empty() ? std::string() : lines.front().get(key);
}

/// La línea `pub` de siempre (la de antes de RFC 0013), armada a mano.
template <class M>
std::string pub_de_siempre(std::uint64_t source_id, const std::string& topic) {
    return "agmen-hub/1 pub id=" + hub::hex16(source_id) + " node=" + hub::quote(hub::node_name()) +
           " pid=" + hub::process_id() + " topic=" + topic + " schema=" + hub::hex16(wire::schema_id_v<M>) +
           " type=" + std::string(M::kName) + " text=" + hub::quote(wire::schema_text<M>());
}

/// La línea `sub` de siempre de un tipo compact (sin tcp), armada a mano.
template <class M>
std::string sub_de_siempre(const std::string& id, const std::string& topic, std::uint16_t port) {
    return "agmen-hub/1 sub id=" + id + " node=" + hub::quote(hub::node_name()) + " pid=" + hub::process_id() +
           " topic=" + topic + " schema=" + hub::hex16(wire::schema_id_v<M>) + " type=" + std::string(M::kName) +
           " port=" + std::to_string(port);
}

/// El texto del WireError que lanza `f`; nullopt si no lanza.
template <class F>
std::optional<std::string> wire_error(F&& f, wire::WireError::Kind kind = wire::WireError::Kind::HubRejected) {
    try {
        f();
    } catch (const wire::WireError& e) {
        CHECK(e.kind() == kind);
        return std::string(e.what());
    }
    return std::nullopt;
}

// ---------------------------------------------------------------------------

void test_constantes_y_formato() {
    CHECK(hub::kJointNameMax == 64);
    CHECK(hub::kJointNamesMax == 8192);
    CHECK(hub::kMaxDatagram == 65507);
    CHECK_EQ(hub::format_joint_names({"_a", "b/c.d-1", "9"}), "_a,b/c.d-1,9");
    CHECK_EQ(hub::quote(hub::format_joint_names({"_a", "b/c.d-1", "9"})), "_a,b/c.d-1,9");   // sin %XX
    // El número de articulaciones del esquema: el f32[N] position de primer nivel.
    CHECK(hub::schema_joint_count(wire::schema_text<m::JointState<12>>()) == std::optional<std::size_t>(12));
    CHECK(hub::schema_joint_count(wire::schema_text<m::JointCommand<2>>()) == std::optional<std::size_t>(2));
    CHECK(hub::schema_joint_count("agmen/1 X{f32[3] position}") == std::optional<std::size_t>(3));
    CHECK(!hub::schema_joint_count("agmen/1 X{f64[3] position}"));
    CHECK(!hub::schema_joint_count("agmen/1 X{f32[03] position}"));
    CHECK(!hub::schema_joint_count("agmen/1 X{Y{f32[3] position} a}"));   // no es de primer nivel
    CHECK(!hub::schema_joint_count("agmen/1 X{f32[3] positions}"));
    CHECK(!hub::schema_joint_count(""));
    CHECK(!hub::schema_joint_count("{"));
    CHECK(!hub::schema_joint_count("X{}"));
}

void test_listas_que_no_valen() {
    struct Caso {
        std::vector<std::string> names;
        std::string texto;
    };
    const std::string a65(65, 'a');
    std::vector<std::string> quinientos;
    for (int i = 0; i < 500; ++i) {
        char buf[32];
        std::snprintf(buf, sizeof buf, "articulacion_%04d", i);
        quinientos.emplace_back(buf);
    }
    const auto invalido = [](const std::string& show, const std::string& why) {
        return "names[0] = " + show + " no es un nombre de articulación válido (" + why + "). " + kHelp;
    };
    const std::vector<Caso> casos = {
        {{a65}, invalido("'" + std::string(64, 'a') + "…'", "mide 65 caracteres y el máximo es 64")},
        {quinientos, "names ocupa 8999 caracteres (los 500 nombres separados por comas) y el máximo es 8192: usa "
                     "nombres más cortos o reparte las articulaciones en varios grupos (un topic por grupo)"},
        {{"a", "b", "a"},
         "'a' está dos veces en names (names[0] y names[2]). Cada articulación va una sola vez: quita una o "
         "corrige el nombre"},
        {{"a b"}, invalido("'a b'", "tiene un espacio")},
        {{"a,b"}, invalido("'a,b'", "tiene una coma")},
        {{"a=b"}, invalido("'a=b'", "tiene un '='")},
        {{"ñ"}, invalido("'ñ'", "tiene 'ñ'")},
        {{"-a"}, invalido("'-a'", "empieza con '-'; el primer carácter es una letra, un dígito o '_'")},
        {{"a\x1b[31m"}, invalido("'a?[31m'", "tiene un carácter de control (0x1b)")},
        {{""}, invalido("''", "está vacío")},
        {{"x\u200By"}, invalido("'x?y'", "tiene un carácter que no se ve (U+200B)")},
        {{"a'b"}, invalido("\"a'b\"", "tiene '''")},
        {{"a\\b"}, invalido("'a\\\\b'", "tiene '\\'")},
        {{std::string(70, 'b') + " "}, invalido("'" + std::string(64, 'b') + "…'", "tiene un espacio")},
        {{"a\xff"}, invalido("'a?'", "tiene un byte que no es UTF-8 (0xff)")},   // solo en C++
    };
    for (const auto& c : casos) {
        // hub::check_joint_names: el texto de Python (sin "Publisher en ...").
        try {
            hub::check_joint_names(c.names);
            CHECK(false);   // check_joint_names no lanzó
        } catch (const std::invalid_argument& e) {
            CHECK_EQ(e.what(), c.texto);
        }
        // Publisher y Subscriber: con quién y dónde delante, como joint_names_for de Python.
        FakeHub h;
        const auto pub = wire_error([&] {
            agmen::PublisherCore p("/patas/estado", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "",
                                   agmen::TopicOptions{c.names});
        });
        CHECK(pub.has_value());
        if (pub) CHECK_EQ(*pub, "Publisher en '/patas/estado': " + c.texto);
        const auto sub = wire_error([&] {
            agmen::SubscriberCore s("/patas/estado", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false},
                                    agmen::TopicOptions{c.names});
        });
        CHECK(sub.has_value());
        if (sub) CHECK_EQ(*sub, "Subscriber en '/patas/estado': " + c.texto);
        CHECK(!h.any("pub", 50ms));   // nada se registró
    }
    // Vacía en hub::check_joint_names es un error; en TopicOptions, vacía = sin lista.
    try {
        hub::check_joint_names({});
        CHECK(false);   // check_joint_names({}) no lanzó
    } catch (const std::invalid_argument& e) {
        CHECK_EQ(e.what(), "names está vacía: pon los nombres de las articulaciones en orden, ej. "
                           "[\"cadera\", \"rodilla\"] (o no la pases)");
    }
    // Una lista justo en el tope sí entra, y un carácter más no.
    std::vector<std::string> justo;
    std::size_t largo = 0;
    while (largo + 64 + 1 <= hub::kJointNamesMax) {
        justo.push_back(std::string(60, 'x') + std::to_string(1000 + justo.size()));
        largo += justo.back().size() + (justo.size() > 1 ? 1 : 0);
    }
    justo.push_back(std::string(hub::kJointNamesMax - largo - 1, 'y'));
    CHECK(hub::format_joint_names(justo).size() == hub::kJointNamesMax);
    hub::check_joint_names(justo);
    justo.back() += 'z';
    bool lanzo = false;
    try {
        hub::check_joint_names(justo);
    } catch (const std::invalid_argument& e) {
        lanzo = std::string(e.what()).find("el máximo es 8192") != std::string::npos;
    }
    CHECK(lanzo);
}

void test_numero_del_tipo_y_direccion() {
    FakeHub h;
    const std::vector<std::string> diez(kPatas.begin(), kPatas.begin() + 10);
    auto e = wire_error([&] { agmen::Publisher<m::JointCommand<12>> p("/patas/cmd", {.names = diez}); });
    CHECK(e.has_value());
    if (e) {
        CHECK_EQ(*e, "Publisher en '/patas/cmd': names tiene 10 nombres y JointCommand es de 12 articulaciones "
                     "(f32[12] position): pasa un nombre por articulación, en el orden de los arreglos");
    }
    e = wire_error([&] { agmen::Subscriber<m::JointState<2>> s("/x", {.names = {"a"}}); });
    CHECK(e.has_value());
    if (e) {
        CHECK_EQ(*e, "Subscriber en '/x': names tiene 1 nombre y JointState es de 2 articulaciones (f32[2] "
                     "position): pasa un nombre por articulación, en el orden de los arreglos");
    }
    // Con dirección fija no hay hub a quien declarársela.
    e = wire_error([&] { agmen::Subscriber<m::JointState<1>> s("127.0.0.1:0", {.names = {"a"}}); });
    CHECK(e.has_value());
    if (e) {
        CHECK_EQ(*e, "Subscriber: names= va con un topic (la lista se le declara al hub, que la compara), y "
                     "'127.0.0.1:0' no es un topic. Usa un topic, ej. '/patas/cmd', o no pases names=");
    }
    e = wire_error([&] { agmen::Publisher<m::JointState<1>> p("127.0.0.1:7999", {.names = {"a"}}); });
    CHECK(e.has_value() && e->rfind("Publisher: names= va con un topic", 0) == 0);
    e = wire_error([&] { agmen::Subscriber<m::JointState<1>> s("*", {.names = {"a"}}); });
    CHECK(e.has_value() && e->find("'*' no es un topic") != std::string::npos);
    // Un topic mal escrito: el error de siempre.
    e = wire_error([&] { agmen::Publisher<m::JointState<1>> p("/mal//topic", {.names = {"a"}}); });
    CHECK(e.has_value() && e->rfind("nombre de topic inválido '/mal//topic'", 0) == 0);
    // Un tipo sin f32[N] position manda la lista igual (no hay número que revisar).
    {
        agmen::PublisherCore p("/x", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "",
                               agmen::TopicOptions{{"a", "b", "c"}});
        const auto line = h.next("pub");
        CHECK_EQ(field(line, "names"), "a,b,c");
    }
    CHECK(!h.any("pub", 50ms));   // ninguno de los que fallaron se registró
}

void test_registro_que_no_cabe_en_un_datagrama() {
    FakeHub h;
    std::string text = "agmen/1 Grande{";
    for (int i = 0; i < 2900; ++i) {
        char buf[32];
        std::snprintf(buf, sizeof buf, "%sf32 campo_%05d", i == 0 ? "" : ";", i);
        text += buf;
    }
    text += "}";
    const agmen::TypeInfo grande{"Grande", text, 7, 2900 * 4, false};
    std::vector<std::string> nombres;
    for (int i = 0; i < 1500; ++i) nombres.push_back("n" + std::to_string(i));
    const auto e = wire_error([&] { agmen::PublisherCore p("/grande", grande, "", agmen::TopicOptions{nombres}); });
    CHECK(e.has_value());
    if (e) {
        CHECK(e->rfind("Publisher en '/grande': el registro en el hub mide 6", 0) == 0);
        const auto names_bytes = hub::quote(hub::format_joint_names(nombres)).size();
        CHECK(e->find(" bytes y en un datagrama caben 65507 (lo que más ocupa, escrito como va en la línea: el nombre "
                      "del nodo, 14 bytes; el esquema de Grande, " +
                      std::to_string(hub::quote(text).size()) + " bytes; names, " + std::to_string(names_bytes) +
                      " bytes). Lo más largo es el esquema de Grande: usa un tipo más chico que Grande") !=
              std::string::npos);
        CHECK(e->find("muy largo") == std::string::npos);
    }
    CHECK(!h.any("pub", 50ms));
    agmen::PublisherCore corto("/grande", grande, "", agmen::TopicOptions{{"a"}});   // con una lista corta, sí cabe
    CHECK_EQ(field(h.next("pub"), "names"), "a");
}

/// Con o sin names, el registro de un Publisher o un Subscriber de topic debe caber en un datagrama: con un
/// nombre de nodo enorme no se crea (antes, el Subscriber se creaba, su registro nunca salía y el usuario veía
/// "no hay un agmen hub", que es falso). El texto es el mismo que el de Python (python/test_nombres.py lo compara).
void test_registro_que_no_cabe_por_el_nombre_del_nodo() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    // 70000 bytes en la línea con 23 336 caracteres: cabe en una variable de entorno de Windows (70 000 'n' no).
    const std::string enorme = nodo_que_mide(70000);
    set_env("AGMEN_NODE", enorme.c_str());
    const std::string fix = "Lo más largo es el nombre del nodo: usa un nombre de nodo más corto (AGMEN_NODE, o el "
                            "name de su [[node]] en robot.toml)";
    const auto esquema = std::to_string(hub::quote(wire::schema_text<Cmd>()).size());
    {
        const auto e = wire_error([&] { agmen::Publisher<Cmd> p("/x"); });
        CHECK(e.has_value());
        if (e) {
            CHECK(e->rfind("Publisher en '/x': el registro en el hub mide 7", 0) == 0);
            CHECK(e->find(" bytes y en un datagrama caben 65507 (lo que más ocupa, escrito como va en la línea: el "
                          "nombre del nodo, 70000 bytes; el esquema de JointCommand, " +
                          esquema + " bytes). " + fix) != std::string::npos);
        }
    }
    {
        const auto e = wire_error([&] { agmen::Subscriber<Cmd> s("/x"); });
        CHECK(e.has_value());
        if (e) {
            CHECK(e->rfind("Subscriber en '/x': el registro en el hub mide 7", 0) == 0);
            CHECK(e->find(" bytes y en un datagrama caben 65507 (lo que más ocupa, escrito como va en la línea: el "
                          "nombre del nodo, 70000 bytes). " + fix) != std::string::npos);
        }
    }
    {
        const auto e = wire_error([&] { agmen::Subscriber<Cmd> s("/x", {.names = {"a", "b"}}); });
        CHECK(e.has_value());
        if (e) {
            CHECK(e->find("(lo que más ocupa, escrito como va en la línea: el nombre del nodo, 70000 bytes; names, 3 "
                          "bytes). " + fix) != std::string::npos);
        }
    }
    {   // Node::subscribe (el mismo SubscriberCore)
        agmen::Node node("enorme");
        const auto e = wire_error([&] { node.subscribe<Cmd>("/x", [](const Cmd&) {}); });
        CHECK(e.has_value());
    }
    CHECK(!h.any("pub", 50ms));
    CHECK(!h.any("sub", 50ms));
    // Con dirección fija no hay registro: no se revisa.
    { agmen::Subscriber<Cmd> s("127.0.0.1:0"); }
    // Un nombre de nodo largo que sí cabe: la línea de siempre, byte a byte (59800 bytes en la línea).
    const std::string largo = nodo_que_mide(60000 - 200);
    set_env("AGMEN_NODE", largo.c_str());
    {
        agmen::Publisher<Cmd> p("/x");
        CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(p.source_id(), "/x"));
    }
    set_env("AGMEN_NODE", "prueba-nombres");
}

/// El siguiente latido de `p` (heartbeat() late cada segundo) termina en `tail`.
template <class M>
bool until_beat(FakeHub& h, agmen::Publisher<M>& p, const std::string& tail) {
    const auto end = std::chrono::steady_clock::now() + 3s;
    while (std::chrono::steady_clock::now() < end) {
        p.heartbeat();
        const auto line = h.next("pub", 100ms);
        if (!line.empty()) return line.size() >= tail.size() && line.substr(line.size() - tail.size()) == tail;
    }
    return false;
}

void test_la_linea_sin_names_es_la_de_siempre_y_con_names_la_de_python() {
    using Cmd = m::JointCommand<12>;
    using Est = m::JointState<3>;
    FakeHub h;
    {
        agmen::Publisher<Cmd> p("/patas/cmd");
        CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(p.source_id(), "/patas/cmd"));
    }
    {
        agmen::Publisher<Cmd> p("/patas/cmd", agmen::TopicOptions{});   // vacía = sin lista
        CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(p.source_id(), "/patas/cmd"));
    }
    {
        agmen::Publisher<Cmd> p("/patas/cmd", {.names = kPatas});
        CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(p.source_id(), "/patas/cmd") + " names=" + kPatasText);
        // Cada latido lleva la lista.
        CHECK(until_beat(h, p, " names=" + kPatasText));
    }
    {
        agmen::Subscriber<Est> s("/brazo/estado");
        const auto line = h.next("sub");
        CHECK_EQ(line, sub_de_siempre<Est>(field(line, "id"), "/brazo/estado", s.local_port()));
    }
    {
        agmen::Subscriber<Est> s("/brazo/estado", {.names = {"hombro", "codo", "mano.1"}});
        const auto line = h.next("sub");
        CHECK_EQ(line, sub_de_siempre<Est>(field(line, "id"), "/brazo/estado", s.local_port()) +
                           " names=hombro,codo,mano.1");
    }
}

/// Un texto de rechazo como los del hub con listas largas (~1100 caracteres, con acentos y signos que van
/// con %XX): debe llegar entero.
std::string rechazo_largo() {
    std::string msg = "'/patas/cmd': 'política' declara ";
    for (int i = 0; i < 40; ++i) msg += "articulación_larga_" + std::to_string(i) + ", ";
    msg += "… y robot.toml dice otra cosa: pasa names= en el orden de [[joints]] de robot.toml (fin)";
    return msg;
}

void test_el_rechazo_del_hub_llega_entero() {
    using Cmd = m::JointCommand<12>;
    const auto msg = rechazo_largo();
    CHECK(msg.size() > 1000);
    FakeHub h;
    {
        agmen::Publisher<Cmd> p("/patas/cmd", {.names = kPatas});
        const auto line = h.next("pub");
        h.reply(hub::line("error", {{"id", field(line, "id")}, {"msg", msg}}));
        std::optional<std::string> e;
        const auto end = std::chrono::steady_clock::now() + 3s;
        while (!e && std::chrono::steady_clock::now() < end) {
            e = wire_error([&] { (void)p.publish(Cmd{}); });
            std::this_thread::sleep_for(10ms);
        }
        CHECK(e.has_value());
        if (e) CHECK_EQ(*e, "el hub rechazó publicar en '/patas/cmd': " + msg);
    }
    {
        agmen::Subscriber<Cmd> s("/patas/cmd", {.names = kPatas});
        const auto line = h.next("sub");
        h.reply(hub::line("error", {{"id", field(line, "id")}, {"msg", msg}}));
        std::optional<std::string> e;
        const auto end = std::chrono::steady_clock::now() + 3s;
        while (!e && std::chrono::steady_clock::now() < end) e = wire_error([&] { (void)s.receive(50ms); });
        CHECK(e.has_value());
        if (e) CHECK_EQ(*e, "el hub rechazó escuchar '/patas/cmd': " + msg);
    }
}

void test_node_con_names() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    agmen::Node node("nombres");
    auto p = node.publisher<Cmd>("/n/cmd", {.names = {"cadera", "rodilla"}});
    CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(p.raw().source_id(), "/n/cmd") + " names=cadera,rodilla");
    node.subscribe<Cmd>("/n/cmd", [](const Cmd&) {}, {.names = {"cadera", "rodilla"}});
    CHECK_EQ(field(h.next("sub"), "names"), "cadera,rodilla");
    node.subscribe<Cmd>("/n/cmd", [](const Cmd&) {}, {.queue = 3});   // sin lista: la de siempre
    CHECK(h.next("sub").find("names") == std::string::npos);
    auto e = wire_error([&] { node.subscribe<Cmd>("/n/cmd", [](const Cmd&) {}, {.names = {"a", "a"}}); });
    CHECK(e.has_value() && e->find("'a' está dos veces en names") != std::string::npos);
    e = wire_error([&] { (void)node.publisher<Cmd>("/n/cmd", {.names = {"a"}}); });
    CHECK(e.has_value() && e->find("names tiene 1 nombre y JointCommand es de 2") != std::string::npos);
}

/// El código de usuario de antes compila y se porta igual: sin TopicOptions nada cambia.
void test_el_codigo_de_antes_compila() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    agmen::Publisher<Cmd> a("/antes");
    CHECK(a.topic() == "/antes");
    agmen::Publisher<Cmd> b("127.0.0.1:7999", "/etiqueta");   // dirección + etiqueta (const char*)
    CHECK(b.topic() == "/etiqueta");
    agmen::Publisher<Cmd> c("127.0.0.1:7999", std::string("/otra"));
    CHECK(c.topic() == "/otra");
    agmen::Publisher<Cmd> d("127.0.0.1:7999");
    CHECK(d.topic() == "/JointCommand");
    agmen::Subscriber<Cmd> s("/antes");
    agmen::Subscriber<Cmd> t("127.0.0.1:0");
    agmen::Node node;
    auto np = node.publisher<Cmd>("/antes");
    auto nd = node.publisher<Cmd>("127.0.0.1:7999");
    CHECK(nd.raw().topic() == "/JointCommand");
    node.subscribe<Cmd>("/antes", [](const agmen::Sample<Cmd>&) {});
    node.subscribe<Cmd>("/antes", [](const Cmd&) {}, {.queue = 5});
    node.subscribe<Cmd>("/antes", [](const Cmd&) {}, {.queue = 5, .group = agmen::Group{}});
    node.subscribe<Cmd>("/antes", [](const Cmd&) {}, agmen::SubscribeOptions{2, {}});
    agmen::SubscribeOptions opt;
    opt.queue = 4;
    node.subscribe<Cmd>("/antes", [](const Cmd&) {}, opt);
    agmen::PublisherCore pc("/antes", agmen::TypeInfo::of<Cmd>());
    agmen::SubscriberCore sc("/antes", agmen::TypeInfo::of<Cmd>());
    int registros = 0;   // los de arriba por topic (3 pub y 6 sub, más sus latidos), ninguno con names
    for (std::string line = h.next("", 500ms); !line.empty(); line = h.next("", 300ms)) {
        const auto op = line.substr(hub::kPrefix.size() + 1, 4);
        if (op != "pub " && op != "sub ") continue;
        CHECK(line.find("names") == std::string::npos);
        ++registros;
    }
    CHECK(registros >= 9);
}


// ---------------------------------------------------------------------------
// Ataques (tester): entradas feas, copias, hilos, fugas y la línea de siempre.

/// Los dígitos de i en base 63 con el primer signo siempre válido ("a", "b", ..., "aa", "ab"...): nombres cortos
/// y distintos.
std::string nombre_corto(std::size_t i) {
    static const char kAbc[] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_";
    constexpr std::size_t base = sizeof kAbc - 1;
    std::string out;
    do {
        out += kAbc[i % base];
        i /= base;
    } while (i != 0);
    return out;
}

/// Los fd abiertos de este proceso (solo en Linux; -1 si no se sabe).
int fds_abiertos() {
#ifdef __linux__
    int n = 0;
    std::error_code ec;
    for (const auto& e : std::filesystem::directory_iterator("/proc/self/fd", ec)) {
        (void)e;
        ++n;
    }
    return ec ? -1 : n;
#else
    return -1;
#endif
}

void test_atacan_nombres_con_bytes_raros() {
    FakeHub h;
    const std::vector<std::string> malos = {
        std::string("a\0b", 3),               // byte 0 en medio
        std::string("\0", 1),                 // solo byte 0
        std::string("ab\0", 3),               // byte 0 al final (c_str() lo escondería)
        "caf\xC3",                            // UTF-8 cortado
        "\xC0\xAF",                           // sobrelargo
        "\xED\xA0\x80",                       // sustituto
        "\xF4\x90\x80\x80",                   // arriba de U+10FFFF
        "\xFF\xFE",                           // BOM al revés
        "a\r\nagmen-hub/1 bye id=x",          // intento de meter otra línea en el datagrama
        "a\tb", "a\nb", "a%2Cb", "a%", "%41", // control, % sin escapar
        " nbsp", " sep", "﻿bom", "á",    // espacios raros y marcas
    };
    for (const auto& n : malos) {
        for (const auto& lista : {std::vector<std::string>{n}, std::vector<std::string>{"ok", n},
                                  std::vector<std::string>{n, "ok"}}) {
            const auto pub = wire_error([&] { agmen::PublisherCore p("/x", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "", agmen::TopicOptions{lista}); });
            CHECK(pub.has_value());
            if (!pub) std::fprintf(stderr, "    (nombre aceptado: %zu bytes, primer byte 0x%02x)\n", n.size(), static_cast<unsigned>(static_cast<unsigned char>(n[0])));
            if (pub) {
                CHECK(pub->find('\0') == std::string::npos);                 // el error no lleva el byte 0
                CHECK(pub->find('\x1b') == std::string::npos);
                CHECK(pub->size() < 1000);                                    // acotado
                for (char c : *pub) CHECK(c == '\n' ? false : (static_cast<unsigned char>(c) >= 0x20 || static_cast<unsigned char>(c) >= 0x80));
            }
        }
    }
    CHECK(!h.any("pub", 100ms));   // nada se registró
}

void test_atacan_topes_de_nombre_y_lista() {
    using Big = m::JointState<2>;
    FakeHub h;
    // Un nombre de 64 caracteres entra; uno de 65, no (el error nombra el tope).
    {
        agmen::Publisher<Big> p("/t64", {.names = {std::string(64, 'a'), "b"}});
        CHECK_EQ(field(h.next("pub"), "names"), std::string(64, 'a') + ",b");
    }
    {
        const auto e = wire_error([&] { agmen::Publisher<Big> p("/t65", {.names = {std::string(65, 'a'), "b"}}); });
        CHECK(e.has_value() && e->find("mide 65 caracteres y el máximo es 64") != std::string::npos);
    }
    // Un nombre de 64 con el primer carácter malo sigue siendo malo; 64 de '-' no empieza bien.
    {
        const auto e = wire_error([&] { agmen::Publisher<Big> p("/tm", {.names = {std::string(64, '-'), "b"}}); });
        CHECK(e.has_value());
    }
    // Una lista de exactamente 8192 caracteres entra por Publisher y por Subscriber, y llega entera al hub.
    std::vector<std::string> justo;
    std::size_t largo = 0;   // con las comas
    for (std::size_t i = 0;; ++i) {
        const std::size_t resta = hub::kJointNamesMax - largo - (justo.empty() ? 0 : 1);   // lo que queda para nombres
        if (resta <= 64) {
            auto ult = nombre_corto(i) + std::string(64, 'y');
            ult.resize(resta);
            justo.push_back(std::move(ult));
            break;
        }
        auto n = nombre_corto(i) + std::string(64, 'x');
        n.resize(std::min<std::size_t>(64, resta - 2));
        largo += n.size() + (justo.empty() ? 0 : 1);
        justo.push_back(std::move(n));
    }
    CHECK(hub::format_joint_names(justo).size() == hub::kJointNamesMax);
    {
        agmen::PublisherCore p("/t8192", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "", agmen::TopicOptions{justo});
        CHECK_EQ(field(h.next("pub"), "names"), hub::format_joint_names(justo));
        agmen::SubscriberCore s("/t8192", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, agmen::TopicOptions{justo});
        CHECK_EQ(field(h.next("sub"), "names"), hub::format_joint_names(justo));
    }
    justo.back() += 'z';   // 8193
    {
        const auto e = wire_error([&] { agmen::PublisherCore p("/t8193", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "", agmen::TopicOptions{justo}); });
        CHECK(e.has_value() && e->find("names ocupa 8193 caracteres") != std::string::npos);
        const auto e2 = wire_error([&] { agmen::SubscriberCore s("/t8193", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, agmen::TopicOptions{justo}); });
        CHECK(e2.has_value() && e2->find("names ocupa 8193 caracteres") != std::string::npos);
    }
    CHECK(!h.any("pub", 50ms));
    CHECK(!h.any("sub", 50ms));
}

void test_atacan_5453_articulaciones() {
    // JointState<5453>: ¿cabe? Con los nombres más cortos posibles la lista pasa de 8192 caracteres: debe dar el
    // error de siempre (qué pasó y qué hacer), no tirar nada ni mandar nada.
    FakeHub h;
    std::vector<std::string> nombres;
    for (std::size_t i = 0; i < 5453; ++i) nombres.push_back(nombre_corto(i));
    const auto e = wire_error([&] { agmen::Publisher<m::JointState<5453>> p("/enorme", {.names = nombres}); });
    CHECK(e.has_value());
    if (e) {
        CHECK(e->find("el máximo es 8192") != std::string::npos);
        CHECK(e->find("usa nombres más cortos o reparte las articulaciones en varios grupos") != std::string::npos);
        CHECK(e->size() < 600);
    }
    // Con el número que no es: antes el error de la lista o el del número? (Python revisa la lista primero.)
    nombres.pop_back();
    const auto e2 = wire_error([&] { agmen::Publisher<m::JointState<5453>> p("/enorme", {.names = nombres}); });
    CHECK(e2.has_value());
    if (e2) CHECK(e2->find("el máximo es 8192") != std::string::npos);
    CHECK(!h.any("pub", 100ms));
}

void test_atacan_repetido_al_final_de_una_lista_larga() {
    FakeHub h;
    std::vector<std::string> nombres;
    for (std::size_t i = 0; i < 1500; ++i) nombres.push_back(nombre_corto(i));
    CHECK(hub::format_joint_names(nombres).size() < hub::kJointNamesMax);
    nombres.push_back(nombres.front());
    const auto e = wire_error([&] { agmen::PublisherCore p("/larga", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "", agmen::TopicOptions{nombres}); });
    CHECK(e.has_value());
    if (e) CHECK(e->find("'a' está dos veces en names (names[0] y names[1500])") != std::string::npos);
    // Mayúsculas distintas no son repetidas.
    agmen::PublisherCore ok("/larga", agmen::TypeInfo{"X", "agmen/1 X{u8 a}", 1, 1, false}, "", agmen::TopicOptions{{"a", "A"}});
    CHECK_EQ(field(h.next("pub"), "names"), "a,A");
}

void test_options_copiadas_y_movidas_y_publisher_movido() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    agmen::TopicOptions o{{"cadera", "rodilla"}};
    agmen::TopicOptions copia = o;
    o.names[0] = "otra";                       // cambiar el original no cambia la copia
    agmen::TopicOptions movida = std::move(copia);
    {
        agmen::Publisher<Cmd> p("/cp", movida);
        CHECK_EQ(field(h.next("pub"), "names"), "cadera,rodilla");
        movida.names.clear();                   // y borrar la fuente no toca al Publisher ya creado
        CHECK(until_beat(h, p, " names=cadera,rodilla"));
        // Un Publisher movido sigue latiendo con su lista.
        agmen::Publisher<Cmd> q(std::move(p));
        CHECK(until_beat(h, q, " names=cadera,rodilla"));
        agmen::Publisher<Cmd> r("/cp2", {.names = {"a", "b"}});
        (void)h.next("pub");
        r = std::move(q);                       // asignar por movimiento: el destino cambia de lista y de topic
        CHECK(r.topic() == "/cp");
        CHECK(until_beat(h, r, " names=cadera,rodilla"));
    }
    // El mismo TopicOptions para un Subscriber y para un Publisher.
    agmen::Subscriber<Cmd> s("/cp", o);
    CHECK_EQ(field(h.next("sub"), "names"), "otra,rodilla");
    // Un Subscriber movido sigue declarándose con su lista.
    agmen::Subscriber<Cmd> s2(std::move(s));
    const auto end = std::chrono::steady_clock::now() + 3s;
    bool vio = false;
    while (!vio && std::chrono::steady_clock::now() < end) {
        (void)s2.receive(100ms);
        const auto line = h.next("sub", 100ms);
        vio = line.find(" names=otra,rodilla") != std::string::npos;
    }
    CHECK(vio);
}

void test_atacan_dos_publishers_mismo_topic_listas_distintas() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    agmen::Publisher<Cmd> a("/mismo", {.names = {"a", "b"}});
    const auto la = h.next("pub");
    agmen::Publisher<Cmd> b("/mismo", {.names = {"b", "a"}});
    const auto lb = h.next("pub");
    CHECK_EQ(field(la, "names"), "a,b");
    CHECK_EQ(field(lb, "names"), "b,a");
    CHECK(field(la, "id") != field(lb, "id"));
    // Cada uno late con su propia lista (la lista no es global ni se pisa).
    for (int i = 0; i < 2; ++i) {
        CHECK(until_beat(h, a, " names=a,b"));
        CHECK(until_beat(h, b, " names=b,a"));
    }
    // Y uno sin lista junto a uno con lista: el de sin lista sigue sin ella.
    agmen::Publisher<Cmd> c("/mismo");
    CHECK(h.next("pub").find("names") == std::string::npos);
    c.heartbeat();
    CHECK(h.next("pub", 2000ms).find("names") == std::string::npos);   // late, sin lista
}

void test_atacan_hilos_creando_publishers_con_lista() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    constexpr int kHilos = 8, kPorHilo = 25;
    std::atomic<int> fallos{0};
    // jthread: si algo lanza en este hilo antes de los join, se esperan al destruirse (un std::thread sin join
    // llamaría a std::terminate y el proceso moriría sin decir en qué prueba).
    std::vector<std::jthread> hilos;
    for (int t = 0; t < kHilos; ++t) {
        hilos.emplace_back([&, t] {
            try {
                for (int i = 0; i < kPorHilo; ++i) {
                    const std::string topic = "/h" + std::to_string(t) + "_" + std::to_string(i);
                    agmen::Publisher<Cmd> p(topic, {.names = {"a" + std::to_string(t), "b" + std::to_string(i)}});
                    (void)p.publish(Cmd{});
                }
            } catch (...) {
                ++fallos;
            }
        });
    }
    // Se leen en este hilo los registros mientras los otros crean (el socket del hub de mentira es de este hilo).
    std::map<std::string, std::string> vistos;   // topic → names
    const auto end = std::chrono::steady_clock::now() + 6s;
    while (std::chrono::steady_clock::now() < end) {
        const auto line = h.next("pub", 200ms);
        if (!line.empty()) vistos[field(line, "topic")] = field(line, "names");
        if (vistos.size() == static_cast<std::size_t>(kHilos * kPorHilo)) break;
    }
    for (auto& t : hilos) t.join();
    CHECK(fallos == 0);
    CHECK(vistos.size() == static_cast<std::size_t>(kHilos * kPorHilo));
    for (int t = 0; t < kHilos; ++t)
        for (int i = 0; i < kPorHilo; ++i) {
            const std::string topic = "/h" + std::to_string(t) + "_" + std::to_string(i);
            CHECK_EQ(vistos[topic], "a" + std::to_string(t) + ",b" + std::to_string(i));   // cada uno, la suya
        }
}

void test_atacan_sin_lista_la_linea_es_la_de_siempre_con_nombre_de_nodo_raro() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    set_env("AGMEN_NODE", "nodo con espacio y ñ=1,2");
    {
        agmen::Publisher<Cmd> p("/raro");
        CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(p.source_id(), "/raro"));
        agmen::Publisher<Cmd> q("/raro", {.names = {"a", "b"}});
        CHECK_EQ(h.next("pub"), pub_de_siempre<Cmd>(q.source_id(), "/raro") + " names=a,b");
    }
    set_env("AGMEN_NODE", "prueba-nombres");
}

void test_atacan_fugas_200_publishers_con_lista() {
    using Cmd = m::JointCommand<2>;
    FakeHub h;
    // Un hilo vacía el socket del hub de mentira mientras se crean y destruyen (si no, el buffer del sistema se
    // llena y se pierden datagramas).
    std::set<std::string> registrados, despedidos;
    std::string error_del_lector;   // lo que lanzó el lector (un hilo no puede dejar escapar una excepción)
    // jthread: si algo lanza en este hilo antes del join, al destruirse pide parar y espera (un std::thread sin
    // join llamaría a std::terminate y el proceso moriría sin decir en qué prueba).
    std::jthread lector([&](std::stop_token parar) {
        try {
            while (!parar.stop_requested()) {
                const auto line = h.next("", 50ms);
                if (line.empty()) continue;
                if (line.rfind("agmen-hub/1 pub ", 0) == 0) {
                    registrados.insert(field(line, "id"));
                    h.reply(hub::line("ok", {{"id", field(line, "id")}}));   // (así el constructor no espera 100 ms)
                } else if (line.rfind("agmen-hub/1 sub ", 0) == 0) {
                    registrados.insert(field(line, "id"));
                    h.reply(hub::line("ok", {{"id", field(line, "id")}}));
                } else if (line.rfind("agmen-hub/1 bye ", 0) == 0) {
                    despedidos.insert(field(line, "id"));
                }
            }
        } catch (const std::exception& e) {
            error_del_lector = e.what();
        }
    });
    {   // calienta: el primer Publisher crea recursos globales que no son fugas
        agmen::Publisher<Cmd> p("/fuga", {.names = {"a", "b"}});
    }
    std::this_thread::sleep_for(200ms);
    const int antes = fds_abiertos();
    for (int i = 0; i < 200; ++i) {
        agmen::Publisher<Cmd> p("/fuga" + std::to_string(i % 7), {.names = {"a", "b"}});
        if (i % 4 == 0) agmen::Subscriber<Cmd> s("/fuga" + std::to_string(i % 7), {.names = {"a", "b"}});
    }
    std::this_thread::sleep_for(300ms);
    const int despues = fds_abiertos();
    if (antes >= 0) {
        if (despues > antes) std::fprintf(stderr, "  fds antes=%d despues=%d\n", antes, despues);
        CHECK(despues <= antes);
    }
    lector.request_stop();
    lector.join();
    if (!error_del_lector.empty()) std::fprintf(stderr, "  el lector lanzó: %s\n", error_del_lector.c_str());
    CHECK(error_del_lector.empty());
    // Cada uno que se registró dice `bye` al irse: ninguno queda en el hub.
    CHECK(registrados.size() >= 200);
    if (registrados != despedidos) {
        std::fprintf(stderr, "  registrados=%zu despedidos=%zu\n", registrados.size(), despedidos.size());
    }
    CHECK(registrados == despedidos);
}

/// Corre una prueba: dice su nombre antes de empezar (con la salida vaciada: si el proceso muere, el log del CI
/// muestra en cuál fue) y, si lanza algo que no atrapó, dice cuál fue y con qué texto, y sigue con las demás.
void correr(const char* nombre, void (*prueba)()) {
    std::printf("-- %s\n", nombre);
    std::fflush(stdout);
    try {
        prueba();
    } catch (const std::exception& e) {
        std::fprintf(stderr, "  FALLA %s: lanzó una excepción que nadie atrapó: %s\n", nombre, e.what());
        ++g_failures;
    } catch (...) {
        std::fprintf(stderr, "  FALLA %s: lanzó algo que no es std::exception\n", nombre);
        ++g_failures;
    }
    // Si la prueba se cortó a la mitad, el nombre del nodo puede haber quedado cambiado: las demás empiezan igual.
    set_env("AGMEN_NODE", "prueba-nombres");
    std::fflush(stderr);
}

}  // namespace

#define CORRER(prueba) correr(#prueba, prueba)

int main() {
    agmen::platform::init_console();
    std::printf("test_nombres\n");
    try {
        set_env("AGMEN_NODE", "prueba-nombres");
    } catch (const std::exception& e) {
        std::fprintf(stderr, "test_nombres: no pude poner AGMEN_NODE: %s\n", e.what());
        return 1;
    }
    CORRER(test_constantes_y_formato);
    CORRER(test_listas_que_no_valen);
    CORRER(test_numero_del_tipo_y_direccion);
    CORRER(test_registro_que_no_cabe_en_un_datagrama);
    CORRER(test_registro_que_no_cabe_por_el_nombre_del_nodo);
    CORRER(test_la_linea_sin_names_es_la_de_siempre_y_con_names_la_de_python);
    CORRER(test_el_rechazo_del_hub_llega_entero);
    CORRER(test_node_con_names);
    CORRER(test_el_codigo_de_antes_compila);
    CORRER(test_atacan_nombres_con_bytes_raros);
    CORRER(test_atacan_topes_de_nombre_y_lista);
    CORRER(test_atacan_5453_articulaciones);
    CORRER(test_atacan_repetido_al_final_de_una_lista_larga);
    CORRER(test_options_copiadas_y_movidas_y_publisher_movido);
    CORRER(test_atacan_dos_publishers_mismo_topic_listas_distintas);
    CORRER(test_atacan_hilos_creando_publishers_con_lista);
    CORRER(test_atacan_sin_lista_la_linea_es_la_de_siempre_con_nombre_de_nodo_raro);
    CORRER(test_atacan_fugas_200_publishers_con_lista);
    if (g_failures == 0) {
        std::printf("test_nombres: todo bien\n");
        return 0;
    }
    std::fprintf(stderr, "test_nombres: %d fallas\n", g_failures);
    return 1;
}
