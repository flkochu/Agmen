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

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

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

void set_env(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value);
#else
    setenv(name, value, 1);
#endif
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
        CHECK(e->rfind("Publisher en '/grande': con names, el registro en el hub mide 6", 0) == 0);
        CHECK(e->find(" bytes y en un datagrama caben 65507 (el esquema de Grande es muy largo): usa nombres más "
                      "cortos o un tipo más chico") != std::string::npos);
    }
    CHECK(!h.any("pub", 50ms));
    agmen::PublisherCore corto("/grande", grande, "", agmen::TopicOptions{{"a"}});   // con una lista corta, sí cabe
    CHECK_EQ(field(h.next("pub"), "names"), "a");
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

}  // namespace

int main() {
    set_env("AGMEN_NODE", "prueba-nombres");
    test_constantes_y_formato();
    test_listas_que_no_valen();
    test_numero_del_tipo_y_direccion();
    test_registro_que_no_cabe_en_un_datagrama();
    test_la_linea_sin_names_es_la_de_siempre_y_con_names_la_de_python();
    test_el_rechazo_del_hub_llega_entero();
    test_node_con_names();
    test_el_codigo_de_antes_compila();
    if (g_failures == 0) std::printf("test_nombres: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
