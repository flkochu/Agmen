# agmen_add_messages(<target> <archivo.agmen>...)
#
# Genera el código de C++ de tus mensajes (RFC 0005) y lo deja listo para usar:
#
#     agmen_add_messages(arca9_msgs mensajes/arca9.agmen)
#     add_executable(mi_nodo mi_nodo.cpp)
#     target_link_libraries(mi_nodo PRIVATE arca9_msgs)     # #include "arca9.hpp"
#
# - Crea una biblioteca INTERFACE <target> con los headers generados
#   (<carpeta de build>/agmen_gen/<target>/<archivo>.hpp) y agmen::core.
# - Se regenera sola cuando cambias un .agmen. Lista también los que importas
#   (import "comunes.agmen"), así CMake sabe que debe vigilarlos.
# - Necesita un Python 3.10 o más nuevo para correr `agmen gen` (el mismo
#   lector que usa Python, así los dos lados nunca se contradicen). No hace
#   falta instalar el paquete: se usa el de esta copia de Agmen.

set(AGMEN_PYTHON_DIR "${CMAKE_CURRENT_LIST_DIR}/../python" CACHE INTERNAL "El paquete de Python de Agmen")

function(agmen_add_messages target)
    set(files ${ARGN})
    if(NOT files)
        message(FATAL_ERROR "agmen_add_messages(${target}): falta al menos un archivo .agmen")
    endif()
    if(NOT Python3_EXECUTABLE)
        find_package(Python3 3.10 COMPONENTS Interpreter)
    endif()
    if(NOT Python3_EXECUTABLE)
        message(FATAL_ERROR
            "agmen_add_messages(${target}) necesita Python 3.10 o más nuevo para generar el código de tus "
            "mensajes. Instálalo (python.org) o indícale a CMake cuál usar: "
            "cmake -S . -B build -DPython3_EXECUTABLE=C:/ruta/a/python.exe")
    endif()

    get_filename_component(python_dir "${AGMEN_PYTHON_DIR}" ABSOLUTE)
    set(out_dir "${CMAKE_CURRENT_BINARY_DIR}/agmen_gen/${target}")
    set(headers "")
    set(inputs "")
    foreach(f IN LISTS files)
        get_filename_component(abs "${f}" ABSOLUTE)
        if(NOT EXISTS "${abs}")
            message(FATAL_ERROR "agmen_add_messages(${target}): no existe '${f}'")
        endif()
        get_filename_component(stem "${f}" NAME_WE)
        list(APPEND headers "${out_dir}/${stem}.hpp")
        list(APPEND inputs "${abs}")
    endforeach()
    # Si cambia el generador (al actualizar Agmen), también se regenera.
    file(GLOB generator "${python_dir}/agmen/idl.py" "${python_dir}/agmen/gen.py" "${python_dir}/agmen/schema.py")

    string(REPLACE ";" " " shown "${files}")
    add_custom_command(
        OUTPUT ${headers}
        COMMAND ${CMAKE_COMMAND} -E env "PYTHONPATH=${python_dir}" "${Python3_EXECUTABLE}" -m agmen gen
                ${inputs} --cpp "${out_dir}" --quiet
        # agmen gen no reescribe un archivo que no cambió; touch le dice a CMake que ya está al día.
        COMMAND ${CMAKE_COMMAND} -E touch ${headers}
        DEPENDS ${inputs} ${generator}
        COMMENT "agmen gen: ${shown}"
        VERBATIM)
    add_custom_target(${target}_gen DEPENDS ${headers})

    add_library(${target} INTERFACE)
    add_dependencies(${target} ${target}_gen)
    target_include_directories(${target} INTERFACE "${out_dir}")
    target_link_libraries(${target} INTERFACE agmen::core)
endfunction()
