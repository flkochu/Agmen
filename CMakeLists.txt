cmake_minimum_required(VERSION 3.20)

project(motio
    VERSION 0.0.1
    DESCRIPTION "Motio: el sistema nervioso para robótica, IA e IoT"
    LANGUAGES CXX)

# ---------------------------------------------------------------------------
# Opciones
# ---------------------------------------------------------------------------
option(MOTIO_BUILD_EXAMPLES "Compilar los ejemplos" ON)
option(MOTIO_BUILD_TESTS    "Compilar las pruebas"  ON)
option(MOTIO_WERROR         "Tratar las advertencias como errores (CI)" OFF)
option(MOTIO_SANITIZE       "Activar AddressSanitizer + UBSan (solo GCC/Clang)" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Tipo de compilación" FORCE)
endif()

# Todos los ejecutables en build/bin, también con Visual Studio (multi-config).
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
foreach(cfg Debug Release RelWithDebInfo MinSizeRel)
    string(TOUPPER ${cfg} CFG)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${CFG} ${CMAKE_BINARY_DIR}/bin)
endforeach()

# ---------------------------------------------------------------------------
# Advertencias: el compilador es nuestro primer revisor de código.
# ---------------------------------------------------------------------------
function(motio_set_warnings target)
    if(MSVC)
        # /utf-8: el código fuente tiene acentos.
        target_compile_options(${target} PRIVATE /W4 /permissive- /utf-8)
        if(MOTIO_WERROR)
            target_compile_options(${target} PRIVATE /WX)
        endif()
    else()
        target_compile_options(${target} PRIVATE
            -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion)
        if(MOTIO_WERROR)
            target_compile_options(${target} PRIVATE -Werror)
        endif()
        if(MOTIO_SANITIZE)
            target_compile_options(${target} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${target} PRIVATE -fsanitize=address,undefined)
        endif()
    endif()
endfunction()

# ---------------------------------------------------------------------------
# Núcleo: motio::core
# ---------------------------------------------------------------------------
add_library(motio_core
    src/wire.cpp
    src/net.cpp
    src/platform.cpp)
add_library(motio::core ALIAS motio_core)

target_include_directories(motio_core PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)
target_compile_features(motio_core PUBLIC cxx_std_20)
motio_set_warnings(motio_core)

if(WIN32)
    # Windows 10+; ws2_32 = sockets, winmm = timeBeginPeriod.
    target_compile_definitions(motio_core PUBLIC _WIN32_WINNT=0x0A00 WIN32_LEAN_AND_MEAN NOMINMAX)
    target_link_libraries(motio_core PUBLIC ws2_32 winmm)
endif()

# ---------------------------------------------------------------------------
# Ejemplos y pruebas
# ---------------------------------------------------------------------------
if(MOTIO_BUILD_EXAMPLES)
    foreach(name publisher subscriber)
        add_executable(${name} examples/nivel0/${name}.cpp)
        target_link_libraries(${name} PRIVATE motio::core)
        motio_set_warnings(${name})
    endforeach()
endif()

if(MOTIO_BUILD_TESTS)
    enable_testing()
    add_executable(test_wire tests/test_wire.cpp)
    target_link_libraries(test_wire PRIVATE motio::core)
    motio_set_warnings(test_wire)
    add_test(NAME wire COMMAND test_wire)
endif()
