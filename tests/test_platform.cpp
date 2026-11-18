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

// Pone la variable con la API estrecha de cada sistema (la misma que usan las
// demás pruebas: en Windows, _putenv_s, que también cambia el entorno del proceso).
void set_env(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value);
#else
    setenv(name, value, 1);
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

#ifdef _WIN32

// Windows: se pone en ancho (UTF-16) en el entorno del proceso, el mismo que lee
// platform::env con GetEnvironmentVariableW, y debe llegar en UTF-8.
void test_windows_llega_en_utf8() {
    // "brazo_ñ": la ñ sí está en la página ANSI occidental (1252), por eso solo
    // con ella una lectura ANSI daría F1 en lugar de C3 B1.
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV", L"brazo_\x00F1") != 0);
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"), "brazo_\xC3\xB1", "brazo_enie");

    // "中" (U+4E2D) no está en la 1252 y el robot (U+1F916) va en un par de
    // sustitutos: con cualquier lectura ANSI saldrían como "?".
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV",
                                  L"brazo_\x00F1"
                                  L"\x4E2D"
                                  L"\xD83E\xDD16") != 0);
    check_bytes(agmen::platform::env("AGMEN_PRUEBA_ENV"),
                "brazo_\xC3\xB1"
                "\xE4\xB8\xAD"
                "\xF0\x9F\xA4\x96",
                "brazo_enie + U+4E2D + U+1F916");

    // Un valor largo (más de lo que cabe en la primera lectura).
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
    CHECK(!avisos.empty() && avisos[0].find("AGMEN_PRUEBA_ENV_MALA") != std::string::npos);

    agmen::log::set_sink(nullptr);
    CHECK(SetEnvironmentVariableW(L"AGMEN_PRUEBA_ENV_MALA", nullptr) != 0);
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
#ifdef _WIN32
    test_windows_llega_en_utf8();
    test_windows_sustituto_suelto_da_vacio_y_avisa_una_vez();
#else
    test_posix_utf8_tal_cual();
#endif
    if (g_failures == 0) std::printf("test_platform: todo bien\n");
    return g_failures == 0 ? 0 : 1;
}
