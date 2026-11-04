cmake_minimum_required(VERSION 3.20)

# La versión vive en un solo lugar: el archivo VERSION (ver docs/VERSIONES.md).
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" AGMEN_VERSION LIMIT_COUNT 1)
# Si cambia VERSION, CMake se vuelve a configurar solo (y agmen::kVersion se actualiza).
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/VERSION")

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
# pip install (scikit-build-core define SKBUILD): solo el módulo de Python.
if(SKBUILD)
    set(AGMEN_DEV_DEFAULT OFF)
else()
    set(AGMEN_DEV_DEFAULT ${AGMEN_TOP_LEVEL})
endif()
option(AGMEN_BUILD_EXAMPLES "Compilar los ejemplos y el tutorial" ${AGMEN_DEV_DEFAULT})
option(AGMEN_BUILD_TESTS    "Compilar las pruebas"                ${AGMEN_DEV_DEFAULT})
option(AGMEN_PYTHON         "Compilar agmen._core, el núcleo de C++ para Python (necesita nanobind)" ${AGMEN_TOP_LEVEL})
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
    src/platform.cpp
    src/hubproto.cpp
    src/clock.cpp
    src/node.cpp
    src/layout.cpp
    src/log.cpp
    src/pubsub.cpp)
add_library(agmen::core ALIAS agmen_core)
# El módulo de Python (agmen._core) es una biblioteca compartida: el núcleo
# tiene que poder ir dentro de ella.
set_target_properties(agmen_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

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

# Mensajes en archivos .agmen → código de C++ (RFC 0005): agmen_add_messages().
include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/AgmenMessages.cmake)

# ---------------------------------------------------------------------------
# agmen._core: el núcleo de C++ para Python (RFC 0007)
# ---------------------------------------------------------------------------
# En el repositorio se compila junto con todo lo demás y queda en python/agmen/,
# así `python -m unittest discover -s python` ya usa el núcleo de C++. Con
# `pip install` lo compila scikit-build-core y va dentro de la wheel.
if(AGMEN_PYTHON OR SKBUILD)
    find_package(Python 3.11 COMPONENTS Interpreter Development.Module
                 OPTIONAL_COMPONENTS Development.SABIModule)
    if(Python_FOUND)
        execute_process(COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
                        OUTPUT_VARIABLE AGMEN_NANOBIND_DIR OUTPUT_STRIP_TRAILING_WHITESPACE
                        RESULT_VARIABLE AGMEN_NANOBIND_RC ERROR_QUIET)
        if(AGMEN_NANOBIND_RC EQUAL 0)
            list(APPEND CMAKE_PREFIX_PATH "${AGMEN_NANOBIND_DIR}")
            find_package(nanobind CONFIG)
        endif()
    endif()
    if(nanobind_FOUND)
        nanobind_add_module(agmen_python STABLE_ABI NB_STATIC python/src/core.cpp)
        set_target_properties(agmen_python PROPERTIES OUTPUT_NAME _core)
        target_link_libraries(agmen_python PRIVATE agmen_core)
        # Las advertencias son para nuestro código, no para las de nanobind.
        target_include_directories(agmen_python SYSTEM PRIVATE ${NB_DIR}/include ${Python_INCLUDE_DIRS})
        if(MSVC)
            target_compile_options(agmen_python PRIVATE /W4 /permissive- /utf-8)
        else()
            agmen_set_warnings(agmen_python)
        endif()
        if(SKBUILD)
            install(TARGETS agmen_python LIBRARY DESTINATION agmen)
        else()
            set_target_properties(agmen_python PROPERTIES LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/python/agmen)
            foreach(cfg Debug Release RelWithDebInfo MinSizeRel)
                string(TOUPPER ${cfg} CFG)
                set_target_properties(agmen_python PROPERTIES
                    LIBRARY_OUTPUT_DIRECTORY_${CFG} ${CMAKE_CURRENT_SOURCE_DIR}/python/agmen)
            endforeach()
        endif()
        message(STATUS "agmen._core (Python ${Python_VERSION}, nanobind ${nanobind_VERSION}): sí")
    elseif(SKBUILD)
        message(FATAL_ERROR "pip install necesita nanobind para compilar agmen._core")
    else()
        message(STATUS "agmen._core: no (falta nanobind: pip install nanobind). Python usará la versión pura.")
    endif()
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
    # motor_command.hpp sale de motor_command.agmen (capítulo 11).
    agmen_add_messages(tutorial_msgs examples/tutorial/motor_command.agmen)
    foreach(name motor controlador)
        add_executable(tutorial_${name} examples/tutorial/${name}.cpp)
        target_link_libraries(tutorial_${name} PRIVATE tutorial_msgs)
        agmen_set_warnings(tutorial_${name})
    endforeach()
    # Un nodo, dos ritmos (capítulo 12, RFC 0006; scripts/paralelo_check.py).
    add_executable(doble_ritmo examples/nodos/doble_ritmo.cpp)
    target_link_libraries(doble_ritmo PRIVATE tutorial_msgs)
    agmen_set_warnings(doble_ritmo)
    # Los mensajes de un perro robot de 12 servos (capítulo 11, scripts/gen_smoke.py).
    agmen_add_messages(arca9_msgs examples/mensajes/arca9.agmen)
    add_executable(arca9_eco examples/mensajes/arca9_eco.cpp)
    target_link_libraries(arca9_eco PRIVATE arca9_msgs)
    agmen_set_warnings(arca9_eco)
endif()

if(AGMEN_BUILD_TESTS)
    enable_testing()
    add_executable(test_wire tests/test_wire.cpp)
    target_link_libraries(test_wire PRIVATE agmen::core)
    # Los mismos vectores de prueba que revisa Python (python/test_vectores.py).
    target_compile_definitions(test_wire PRIVATE
        AGMEN_VECTORS_FILE="${CMAKE_CURRENT_SOURCE_DIR}/tests/vectors/v1.txt")
    agmen_set_warnings(test_wire)
    add_test(NAME wire COMMAND test_wire)

    # Esquemas en tiempo de ejecución (RFC 0007) contra los vectores.
    add_executable(test_layout tests/test_layout.cpp)
    target_link_libraries(test_layout PRIVATE agmen::core)
    target_compile_definitions(test_layout PRIVATE
        AGMEN_VECTORS_FILE="${CMAKE_CURRENT_SOURCE_DIR}/tests/vectors/v1.txt")
    agmen_set_warnings(test_layout)
    add_test(NAME layout COMMAND test_layout)

    # Node y el reloj de Agmen (RFC 0006).
    add_executable(test_node tests/test_node.cpp)
    target_link_libraries(test_node PRIVATE agmen::core)
    agmen_set_warnings(test_node)
    add_test(NAME node COMMAND test_node)

    # El C++ que genera agmen gen pone los mismos bytes que los vectores.
    agmen_add_messages(vectores_msgs tests/vectors/v1.agmen)
    add_executable(test_gen tests/test_gen.cpp)
    target_link_libraries(test_gen PRIVATE vectores_msgs)
    target_compile_definitions(test_gen PRIVATE
        AGMEN_VECTORS_FILE="${CMAKE_CURRENT_SOURCE_DIR}/tests/vectors/v1.txt")
    agmen_set_warnings(test_gen)
    add_test(NAME gen COMMAND test_gen)
endif()
