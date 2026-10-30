cmake_minimum_required(VERSION 3.20)

# La versión vive en un solo lugar: el archivo VERSION (ver docs/VERSIONES.md).
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" AGMEN_VERSION LIMIT_COUNT 1)

project(agmen
    VERSION ${AGMEN_VERSION}
    DESCRIPTION "Agmen: el sistema nervioso para robótica, IA e IoT"
    LANGUAGES CXX)

# ---------------------------------------------------------------------------
# Opciones
# ---------------------------------------------------------------------------
# Ejemplos y pruebas solo por defecto cuando Agmen es el proyecto principal
# (no cuando alguien lo incluye con add_subdirectory en su propio robot).
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    set(AGMEN_TOP_LEVEL ON)
else()
    set(AGMEN_TOP_LEVEL OFF)
endif()
option(AGMEN_BUILD_EXAMPLES "Compilar los ejemplos y el tutorial" ${AGMEN_TOP_LEVEL})
option(AGMEN_BUILD_TESTS    "Compilar las pruebas"                ${AGMEN_TOP_LEVEL})
option(AGMEN_WERROR         "Tratar las advertencias como errores (CI)" OFF)
option(AGMEN_SANITIZE       "Activar AddressSanitizer + UBSan (solo GCC/Clang)" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Tipo de compilación" FORCE)
endif()

# Todos los ejecutables en build/bin, también con Visual Studio (multi-config).
if(AGMEN_TOP_LEVEL)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
    foreach(cfg Debug Release RelWithDebInfo MinSizeRel)
        string(TOUPPER ${cfg} CFG)
        set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${CFG} ${CMAKE_BINARY_DIR}/bin)
    endforeach()
endif()

# ---------------------------------------------------------------------------
# Advertencias: el compilador es nuestro primer revisor de código.
# ---------------------------------------------------------------------------
function(agmen_set_warnings target)
    if(MSVC)
        # /utf-8: el código fuente tiene acentos.
        target_compile_options(${target} PRIVATE /W4 /permissive- /utf-8)
        if(AGMEN_WERROR)
            target_compile_options(${target} PRIVATE /WX)
        endif()
    else()
        target_compile_options(${target} PRIVATE
            -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion)
        if(AGMEN_WERROR)
            target_compile_options(${target} PRIVATE -Werror)
        endif()
        if(AGMEN_SANITIZE)
            target_compile_options(${target} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${target} PRIVATE -fsanitize=address,undefined)
        endif()
    endif()
endfunction()

# ---------------------------------------------------------------------------
# Núcleo: agmen::core
# ---------------------------------------------------------------------------
add_library(agmen_core
    src/wire.cpp
    src/net.cpp
    src/platform.cpp)
add_library(agmen::core ALIAS agmen_core)

# agmen/version.hpp se genera desde VERSION.
configure_file(include/agmen/version.hpp.in ${CMAKE_CURRENT_BINARY_DIR}/generated/agmen/version.hpp @ONLY)

target_include_directories(agmen_core PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/generated>
    $<INSTALL_INTERFACE:include>)
target_compile_features(agmen_core PUBLIC cxx_std_20)
agmen_set_warnings(agmen_core)

if(WIN32)
    # Windows 10+; ws2_32 = sockets, winmm = timeBeginPeriod.
    target_compile_definitions(agmen_core PUBLIC _WIN32_WINNT=0x0A00 WIN32_LEAN_AND_MEAN NOMINMAX)
    target_link_libraries(agmen_core PUBLIC ws2_32 winmm)
endif()

# ---------------------------------------------------------------------------
# Ejemplos y pruebas
# ---------------------------------------------------------------------------
if(AGMEN_BUILD_EXAMPLES)
    foreach(name publisher subscriber)
        add_executable(${name} examples/nivel0/${name}.cpp)
        target_link_libraries(${name} PRIVATE agmen::core)
        agmen_set_warnings(${name})
    endforeach()
    # Código del tutorial (docs/tutorial). Se compila y se prueba en el CI.
    foreach(name motor controlador)
        add_executable(tutorial_${name} examples/tutorial/${name}.cpp)
        target_link_libraries(tutorial_${name} PRIVATE agmen::core)
        agmen_set_warnings(tutorial_${name})
    endforeach()
endif()

if(AGMEN_BUILD_TESTS)
    enable_testing()
    add_executable(test_wire tests/test_wire.cpp)
    target_link_libraries(test_wire PRIVATE agmen::core)
    agmen_set_warnings(test_wire)
    add_test(NAME wire COMMAND test_wire)
endif()
