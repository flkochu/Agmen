// agmen::platform::env: el valor de una variable de entorno, siempre en UTF-8.
//
// En Windows el entorno es UTF-16; antes se leía con _dupenv_s, que lo devuelve
// en la página de códigos ANSI, y un nodo llamado "brazo_ñ" por AGMEN_NODE
// llegaba al hub con bytes que no son UTF-8. Los bytes esperados van escritos
// con escapes (no con letras en el fuente) para no depender de cómo el
// compilador lea la codificación de este archivo.
#include <agmen/log.hpp>
#include <agmen/platform.hpp>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <string_view>
#include <vector>

#ifdef _WIN32
#  include <windows.h>
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

// Pone la variable con la API estrecha de cada sistema (en Windows, _putenv_s,
// que también cambia el entorno del proceso). Si no se pudo poner, lo dice aquí.
void set_env(const char* name, const char* value) {
#ifdef _WIN32
    CHECK(_putenv_s(name, value) == 0);
#else
    CHECK(setenv(name, value, 1) == 0);
#endif
}

void unset_env(const char* name) {
#ifdef _WIN32
    _putenv_s(name, "");
#else
    unsetenv(name);
#endif
}

// Muestra los bytes en hexadecimal, para que una falla diga qué llegó.
std::string hex(std::string_view text) {
    static constexpr char kHex[] = "0123456789ABCDEF";
    std::string out;
    for (char ch : text) {
        const auto c = static_cast<unsigned char>(ch);
        if (!out.empty()) out += ' ';
        out += kHex[c >> 4];
        out += kHex[c & 15];
    }
    return out;
}

void check_bytes(const std::string& got, std::string_view expected, const char* what) {
    if (got != expected) {
        std::fprintf(stderr, "  %s: se esperaba [%s] y llegó [%s]\n", what, hex(expected).c_str(),
                     hex(got).c_str());
        ++g_failures;
    }
}

void test_ascii_se_lee_igual() {
    set_env("AGMEN_PRUEBA_ENV", "brazo_1");
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"), "brazo_1", "ASCII");
    unset_env("AGMEN_PRUEBA_ENV");
}

void test_la_que_no_existe_da_vacio() {
    unset_env("AGMEN_PRUEBA_ENV_NO_EXISTE");
    CHECK(agmen::platform::env("AGMEN_PRUEBA_ENV_NO_EXISTE").empty());
}

// Un nombre nulo no es una variable: "" y sin aviso.
void test_nombre_nulo_da_vacio_sin_avisar() {
    std::vector<std::string> avisos;
    agmen::log::set_sink([&avisos](std::string_view line) { avisos.emplace_back(line); });
    CHECK(agmen::platform::env(nullptr).empty());
    CHECK(avisos.empty());
    agmen::log::set_sink(nullptr);
}

// Un nombre más largo que el búfer de la pila del nombre (64 en Windows).
void test_nombre_largo() {
    const std::string nombre = "AGMEN_PRUEBA_ENV_" + std::string(70, 'L');
    set_env(nombre.c_str(), "largo");
    check_bytes(agmen::platform::env(nombre.c_str()), "largo", "nombre de 87 letras");
    unset_env(nombre.c_str());
    CHECK(agmen::platform::env(nombre.c_str()).empty());
}

#ifdef _WIN32

// Windows: se pone en ancho (UTF-16) con SetEnvironmentVariableW, en el entorno
// del proceso (el que lee platform::env con GetEnvironmentVariableW y el que
// heredan los hijos), y debe llegar en UTF-8, byte por byte.
//
// Lo que esto detecta: (1) leer de otro lado que no sea el entorno del proceso:
// volver a _dupenv_s o std::getenv falla porque leen la copia del CRT, que
// SetEnvironmentVariableW no cambia; (2) convertir a algo que no sea UTF-8: con
// la página ANSI occidental (1252) la ñ saldría F1, y "中" y el robot, "?".
// Lo que no detecta: una lectura ANSI del entorno del proceso
// (GetEnvironmentVariableA) en una máquina con la página ANSI en UTF-8 pasaría.
void test_windows_llega_en_utf8() {
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV", L"brazo_\x00F1") != 0);
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"), "brazo_\xC3\xB1", "brazo_enie");

    // "中" (U+4E2D) no está en la 1252 y el robot (U+1F916) va en un par de
    // sustitutos (cuatro bytes en UTF-8).
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV",
                                  L"brazo_\x00F1"
                                  L"\x4E2D"
                                  L"\xD83E\xDD16") != 0);
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"),
                "brazo_\xC3\xB1"
                "\xE4\xB8\xAD"
                "\xF0\x9F\xA4\x96",
                "brazo_enie + U+4E2D + U+1F916");

    // Justo en el borde del búfer de la pila (256 con el nulo): 255 caracteres
    // caben en la pila; 256 ya van al heap. El último es "中" para comprobar
    // que se convierte entero.
    for (const std::size_t largo_borde : {std::size_t{255}, std::size_t{256}}) {
        std::wstring borde(largo_borde - 1, L'x');
        borde += L'\x4E2D';
        CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV", borde.c_str()) != 0);
        const std::string esperado_borde = std::string(largo_borde - 1, 'x') + "\xE4\xB8\xAD";
        check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"), esperado_borde,
                    largo_borde == 255 ? "255 caracteres" : "256 caracteres");
    }

    // Un valor largo (camino del heap).
    std::wstring largo(1000, L'\x00F1');
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV", largo.c_str()) != 0);
    std::string esperado;
    for (int i = 0; i < 1000; ++i) esperado += "\xC3\xB1";
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"), esperado, "1000 enies");

    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV", nullptr) != 0);
    CHECK(agmen::platform::env("AGMEN_PRUEBA_ENV").empty());
}

// Un sustituto suelto no es UTF-16 válido: no se devuelve basura, se devuelve ""
// y se avisa una sola vez por variable, diciendo cuál.
void test_windows_sustituto_suelto_da_vacio_y_avisa_una_vez() {
    std::vector<std::string> avisos;
    agmen::log::set_sink([&avisos](std::string_view line) { avisos.emplace_back(line); });

    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV_MALA",
                                  L"a"
                                  L"\xD800"
                                  L"b") != 0);
    CHECK(agmen::platform::env("AGMEN_PRUEBA_ENV_MALA").empty());
    CHECK(agmen::platform::env("AGMEN_PRUEBA_ENV_MALA").empty());
    CHECK(avisos.size() == 1);
    if (!avisos.empty()) {
        const std::string& aviso = avisos[0];
        CHECK(aviso.rfind("[agmen] \xE2\x9A\xA0 ", 0) == 0);   // "[agmen] ⚠ "
        CHECK(aviso.find("AGMEN_PRUEBA_ENV_MALA") != std::string::npos);
        CHECK(aviso.find("no es texto v\xC3\xA1lido") != std::string::npos);
        CHECK(aviso.find("vuelve a ponerla") != std::string::npos);
    }

    agmen::log::set_sink(nullptr);
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV_MALA", nullptr) != 0);
}

// Un nombre que no es UTF-8 es un error de quien llama: "", y un aviso que lo dice
// (con el byte malo escrito como \xFF, no crudo).
void test_windows_nombre_que_no_es_utf8() {
    std::vector<std::string> avisos;
    agmen::log::set_sink([&avisos](std::string_view line) { avisos.emplace_back(line); });

    CHECK(agmen::platform::env("AGMEN_PRUEBA_\xFF").empty());
    CHECK(agmen::platform::env("AGMEN_PRUEBA_\xFF").empty());
    CHECK(avisos.size() == 1);
    if (!avisos.empty()) {
        const std::string& aviso = avisos[0];
        CHECK(aviso.rfind("[agmen] \xE2\x9A\xA0 ", 0) == 0);
        CHECK(aviso.find("AGMEN_PRUEBA_\\xFF") != std::string::npos);
        CHECK(aviso.find("error del programa") != std::string::npos);
    }

    agmen::log::set_sink(nullptr);
}

#else

// Linux y macOS: el entorno son bytes; se devuelven tal cual.
void test_posix_utf8_tal_cual() {
    const char* valor =
        "brazo_\xC3\xB1"
        "\xE4\xB8\xAD"
        "\xF0\x9F\xA4\x96";
    set_env("AGMEN_PRUEBA_ENV", valor);
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"), valor, "UTF-8 en POSIX");
    unset_env("AGMEN_PRUEBA_ENV");
}

#endif

}  // namespace

int main() {
    test_ascii_se_lee_igual();
    test_la_que_no_existe_da_vacio();
    test_nombre_nulo_da_vacio_sin_avisar();
    test_nombre_largo();
#ifdef _WIN32
    test_windows_llega_en_utf8();
    test_windows_sustituto_suelto_da_vacio_y_avisa_una_vez();
    test_windows_nombre_que_no_es_utf8();
#else
    test_posix_utf8_tal_cual();
#endif
    if (g_failures == 0) std::printf("test_platform: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
