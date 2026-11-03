# RFC 0005 — Archivos `.agmen`: escribe tus mensajes una vez, Agmen genera el código (nivel 4)

| | |
|---|---|
| **Estado** | 📝 **Propuesta** (2026-11-03). Decisiones abiertas en la [sección 5](#5-decisiones) |
| **Depende de** | RFC 0001 (esquemas en texto, texto canónico, `schema_id`, layout `compact`) |
| **Versión** | Agmen 0.7.0 |

## 1. El problema

Hoy, para tener un mensaje propio en C++ escribes a mano todo esto
(`examples/tutorial/motor_command.hpp`):

```cpp
struct MotorCommand {
    static constexpr std::string_view kName = "MotorCommand";
    static constexpr std::string_view kSchema = "MotorCommand{f32 left;f32 right}";
    static constexpr std::size_t kPayloadLen = 2 * 4;
    float left = 0.0f;
    float right = 0.0f;
    void encode_payload(agmen::wire::Bytes& out) const { put_f32(out, left); put_f32(out, right); }
    static MotorCommand decode_payload(agmen::wire::ByteView p) { /* revisar tamaño, get_f32 ×2 */ }
};
```

Y en Python, otra vez:

```python
@dataclass
class MotorCommand(wire.Message):
    SCHEMA = "MotorCommand{f32 left;f32 right}"
    left: float
    right: float
```

Para dos `f32` funciona. Para los 12 servos de Arca9 con su modo, sus
ganancias y una pose, se vuelve un problema:

1. **Lo mismo dos veces.** El tipo vive en C++ y en Python. Si cambias uno y
   olvidas el otro, Agmen lo detecta (cambia el `schema_id`), pero el error
   aparece hasta que corres el robot.
2. **El código de bytes a mano.** `encode_payload` y `decode_payload` deben
   seguir el layout `compact` exacto: alineación, relleno, el orden de los
   campos. El compilador revisa que el *tamaño* cuadre, pero no que pongas
   `left` antes que `right`.
3. **El texto del esquema es incómodo para tipos grandes.**
   `Arca9{f32[12] q;f32[12] kp;f32[12] kd;u8 modo;Pose{Vec3{f32 x;f32 y;f32 z} p;f32 yaw} base}`
   en una sola línea no se lee, no admite comentarios y no se puede reusar
   `Vec3` en otro mensaje.
4. **No hay constantes ni enums.** `modo = 2` no dice nada; `Modo::TROTAR`, sí.

## 2. Cómo lo resuelven otros

| Sistema | Archivo | Quién genera el código | Detalles |
|---|---|---|---|
| **ROS 2** | `.msg`: un archivo por mensaje, un campo por línea (`float32 x`) | `rosidl_generate_interfaces()` en CMake; los generadores (`rosidl_generator_cpp`, `_py`) están escritos en Python | Constantes (`int32 X=123`, en MAYÚSCULAS), valores por defecto, arreglos fijos, acotados y dinámicos. No tiene enums. Los tipos se refieren como `paquete/Tipo` |
| **Protobuf** | `.proto`: `message Nombre { float x = 1; }`, varios por archivo | `protoc` (binario en C++); en CMake `protobuf_generate()` agrega un `add_custom_command` que regenera al cambiar el `.proto` | Números de campo para evolucionar el esquema; enums; `package` → namespace |
| **FlatBuffers** | `.fbs`: `struct Vec3 { x:float; }`, `table`, `enum Color:byte` | `flatc` | `struct` = tamaño fijo, en línea, no puede cambiar sin romper compatibilidad (igual que `compact` de Agmen); `table` = evoluciona. `namespace` |
| **Agmen hoy** | El texto del esquema dentro del código | Nadie: se escribe a mano | Python arma el layout solo; C++ revisa el esquema en tiempo de compilación (`AGMEN_CHECK_SCHEMA`) |

Lo que se repite: **un archivo con varios tipos, `package`/`namespace`,
enums, y la generación integrada al build** (CMake regenera solo cuando
cambia el archivo). Lo que Agmen ya tiene y los demás no: el texto canónico y
el `schema_id`, que hacen que un tipo se compare **por su estructura**.

## 3. Propuesta

### 3.1 El archivo

```text
# arca9.agmen — los mensajes del perro robot
package arca9                     # C++: namespace arca9 · Python: arca9.Articulaciones

const u8 PATAS = 4                # constante: no viaja en los mensajes

enum Modo : u8 {                  # en los bytes es un u8; en el código, un nombre
    QUIETO = 0
    CAMINAR = 1
    TROTAR = 2
}

struct Vec3 {                     # tipo auxiliar: se usa dentro de otros
    f32 x
    f32 y
    f32 z
}

message Articulaciones {          # se puede publicar
    f32[12] q                     # ángulos de los servos, en grados
    f32[12] kp = 1.0              # valor por defecto (solo en el código)
    Modo modo = CAMINAR
}

message Pose {
    Vec3 p
    f32 yaw
}
```

- Un campo por línea, `tipo nombre`, en el mismo orden y con los mismos tipos
  que el esquema de hoy. `;` al final es opcional. Comentarios con `#`.
- `message` = se puede publicar; `struct` = solo se usa dentro de otros.
- Varios tipos por archivo; `import "comunes.agmen"` para reusar tipos de
  otro archivo.

### 3.2 Lo que NO cambia: los bytes

El archivo es otra forma de **escribir** el esquema; lo que viaja es el mismo
texto canónico de siempre (RFC 0001):

| En el `.agmen` | Texto canónico (lo que se anuncia y de donde sale el `schema_id`) |
|---|---|
| `message Articulaciones { f32[12] q; f32[12] kp = 1.0; Modo modo }` | `agmen/1 Articulaciones{f32[12] q;f32[12] kp;u8 modo}` |
| `message Pose { Vec3 p; f32 yaw }` | `agmen/1 Pose{Vec3{f32 x;f32 y;f32 z} p;f32 yaw}` |

- Un enum viaja como su entero (`u8`), las constantes y los valores por
  defecto no viajan, y el `package` no entra en el texto canónico.
- Por eso un nodo con el struct escrito a mano, uno generado y uno de
  Python hablan entre ellos sin cambiar nada. Y `agmen echo`, el panel y
  Foxglove siguen leyendo cualquier tipo.

### 3.3 El código que se genera

**C++** (`arca9.agmen` → `arca9.hpp`):

```cpp
namespace arca9 {
inline constexpr std::uint8_t PATAS = 4;
enum class Modo : std::uint8_t { QUIETO = 0, CAMINAR = 1, TROTAR = 2 };

struct Articulaciones {
    static constexpr std::string_view kName = "Articulaciones";
    static constexpr std::string_view kSchema = "Articulaciones{f32[12] q;f32[12] kp;u8 modo}";
    std::array<float, 12> q{};
    std::array<float, 12> kp = agmen::filled<float, 12>(1.0f);
    Modo modo = Modo::CAMINAR;
    void encode_payload(agmen::wire::Bytes& out) const;          // generado: alineación y relleno exactos
    static Articulaciones decode_payload(agmen::wire::ByteView);  // generado: revisa tamaño y enums
    friend bool operator==(const Articulaciones&, const Articulaciones&) = default;
};
static_assert(agmen::wire::Message<Articulaciones>);
}  // namespace arca9
```

**Python** (dataclasses, como hoy, con `IntEnum` para los enums):

```python
msgs = agmen.load("arca9.agmen")             # sin paso de compilación
pub = agmen.Publisher(msgs.Articulaciones, "/arca9/q")
pub.publish(msgs.Articulaciones(q=[90.0] * 12, modo=msgs.Modo.TROTAR))
```

**CMake** (se regenera solo cuando cambias el `.agmen`):

```cmake
agmen_add_messages(arca9_msgs arca9.agmen)
target_link_libraries(mi_nodo PRIVATE arca9_msgs)   # #include "arca9.hpp"
```

### 3.4 Errores claros (regla 1)

| Error | Mensaje |
|---|---|
| Tipo con nombre de otro lenguaje | `arca9.agmen:12:5: 'float' no existe en Agmen: usa 'f32'` |
| Tipo desconocido | `arca9.agmen:15:5: tipo desconocido 'Vec'. ¿Quisiste decir 'Vec3'?` |
| Palabra reservada | `arca9.agmen:9:9: el campo 'class' choca con una palabra de C++/Python: usa otro nombre` |
| Tamaño variable | `arca9.agmen:20:5: 'string' es de tamaño variable: llega con la codificación arrow (nivel 7)` |
| Demasiado grande | `'Mapa' mide 70 000 bytes y en un paquete UDP caben 65 443: para datos grandes, nivel 7–8` |
| Un enum con un valor que no existe al decodificar | `'modo' llegó con 7, que no es un Modo (QUIETO, CAMINAR, TROTAR)` |

### 3.5 Herramientas

| Comando | Qué hace |
|---|---|
| `agmen gen arca9.agmen --cpp include/ --python .` | Genera el código (lo usa CMake; también a mano) |
| `agmen gen arca9.agmen --check` | Muestra cada tipo con su texto canónico, `schema_id` y tamaño, sin escribir nada |
| `agmen gen ... --check-files` | Falla si los archivos generados que subiste a git no coinciden con el `.agmen` (para el CI) |

## 4. Lo que NO entra en 0.7.0

- Tipos de tamaño variable (`string`, `T[]`, imágenes): nivel 7 (`arrow`).
- Servicios (pedir y responder): se diseñan aparte.
- Unidades, rangos y descripciones que el panel muestre (`f32 ax @unit("m/s²")`):
  necesitan viajar en el anuncio del esquema, y eso cambia el RFC 0001.
  Queda propuesto para después; las decisiones de hoy no le cierran la puerta.
- Leer archivos `.msg` de ROS 2: va con el puente a ROS 2 (nivel 12).

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Cómo se escribe el archivo? | (a) **Bloques `message Nombre { tipo nombre }`, varios por archivo** (§3.1), (b) estilo ROS `.msg`: un archivo por mensaje, sin llaves, (c) el texto del esquema tal cual, uno por línea | **(a)** | Se lee como C y como Protobuf; reusa tipos (`Vec3`) y agrupa los de un robot en un archivo. (b) es familiar para quien viene de ROS, pero cada tipo anidado es otro archivo y no hay enums. (c) no se lee en tipos grandes |
| **D2** | ¿Quién genera el código de C++? | (a) **`agmen gen` en Python, llamado por CMake (`agmen_add_messages`)**, (b) un generador en C++ compilado por CMake, (c) nadie: generas a mano y subes los archivos a git | **(a)** | Una sola implementación: el lector de esquemas de Python ya existe y pasa los vectores. CMake regenera solo cuando cambia el `.agmen`. Pide Python al compilar, pero Agmen ya lo pide para `agmen run`, y ROS 2 hace lo mismo. (b) evita Python, pero sería un segundo lector que mantener igual al primero. (c) funciona sin nada extra, pero los archivos se quedan viejos sin que nadie lo note (para eso está `--check-files`) |
| **D3** | ¿Y en Python? | (a) **`agmen.load("arca9.agmen")` sin compilar, y `agmen gen --python` si quieres autocompletado**, (b) solo archivos `.py` generados, (c) solo `agmen.load` | **(a)** | Lo cómodo de Python es no compilar: cambias el `.agmen` y vuelves a correr. El `.py` generado sirve para que VS Code autocomplete `msgs.Articulaciones(q=…)`. Las dos salen del mismo lector, así que no se pueden contradecir |
| **D4** | ¿Qué trae el lenguaje en 0.7.0? | (a) **Campos, structs, arreglos fijos, constantes, enums y valores por defecto**, (b) solo campos, structs y arreglos, (c) además unidades y rangos que muestre el panel | **(a)** | Nada de (a) cambia los bytes: un enum viaja como su entero y lo demás vive solo en el código. (c) es útil, pero exige llevar más información en el anuncio del esquema (cambio del RFC 0001); mejor diseñarlo aparte (§4) |
| **D5** | ¿El `package` cuenta para saber si dos tipos son el mismo? | (a) **No: dos tipos son el mismo si su texto canónico es igual**, como hoy, (b) sí: `arca9/Pose` y `brazo/Pose` son distintos aunque tengan los mismos campos | **(a)** | No cambia el formato ni el `schema_id`: el struct escrito a mano y el generado interoperan. Si tu equipo y otro definen `Pose{Vec3 p;f32 yaw}` igual, sus nodos se entienden. Con (b) habría que cambiar el texto canónico (RFC 0001), y un tipo idéntico de otro paquete se rechazaría. Los choques de verdad (mismo topic, otro tipo) ya los detecta el hub |

## 6. Las partes difíciles

| # | Reto | Cómo se enfrenta |
|---|---|---|
| 1 | El C++ generado debe poner los bytes **exactos** (alineación, relleno en 0) | El generador usa el mismo layout de `schema.py`; los tipos de los vectores de prueba se generan y su `encode` en C++ debe dar los mismos bytes que el vector |
| 2 | Que CMake regenere solo cuando cambia el `.agmen` (y los `import`) | `add_custom_command` con `DEPENDS` en el `.agmen` y en lo que importa, como `protobuf_generate` |
| 3 | Encontrar el Python correcto desde CMake (venv, Windows) | `find_package(Python3)` con un mensaje claro si falta: *"agmen_add_messages necesita Python con agmen instalado: pip install agmen"* |
| 4 | Nombres que chocan con palabras reservadas de C++ o Python | Lista de palabras reservadas de los dos lenguajes; error con línea y columna |
| 5 | Enums al decodificar: un nodo más nuevo manda un valor que este no conoce | Error claro con el valor y los nombres válidos (agregar un valor no cambia el `schema_id`, porque el enum viaja como su entero) |
| 6 | Que el ejemplo del tutorial siga funcionando | `motor_command.hpp` pasa a generarse desde `examples/tutorial/tutorial.agmen`; `ImuSample` del núcleo se queda como está |

## 7. Plan para Agmen 0.7.0

1. Lector del archivo `.agmen` (con errores de línea y columna) sobre `schema.py`.
2. `agmen gen` para C++ y Python, y `agmen.load()`.
3. `agmen_add_messages()` en CMake.
4. Pruebas: los tipos de los vectores generados en C++ dan los mismos bytes;
   C++ generado ↔ Python con `agmen.load` (interop); errores.
5. El tutorial pasa a `tutorial.agmen`; capítulo 11.

## Fuentes

- Interfaces de ROS 2 (`.msg`, constantes, valores por defecto, arreglos acotados): <https://docs.ros.org/en/jazzy/Concepts/Basic/About-Interfaces.html> · formato `.msg`: <https://design.ros2.org/articles/legacy_interface_definition.html> · C++ generado: <https://design.ros2.org/articles/generated_interfaces_cpp.html>
- `protobuf_generate` (genera en el build con `add_custom_command`): <https://cmake.org/cmake/help/latest/module/FindProtobuf.html> · <https://github.com/protocolbuffers/protobuf/blob/main/docs/cmake_protobuf_generate.md>
- Esquemas de FlatBuffers (`struct` de tamaño fijo, `table`, `enum`, `namespace`): <https://github.com/google/flatbuffers/blob/master/docs/source/tutorial.md> · un `struct` no puede cambiar de layout sin romper compatibilidad: <https://github.com/dvidelabs/flatcc/blob/master/doc/binary-format.md>
