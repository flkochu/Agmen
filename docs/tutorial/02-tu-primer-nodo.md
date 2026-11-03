# Capítulo 2 — Tu primer nodo en C++

*Disponible desde Agmen 0.1.0 · ⏱ 30–45 minutos*

Vas a crear **tu propio proyecto** con dos nodos:

- **`controlador`**: decide qué tan rápido debe girar cada rueda.
- **`motor`**: recibe esas órdenes y mueve las ruedas. Tiene un **freno de
  seguridad**: si deja de recibir órdenes, se detiene solo.

Y vas a definir **tu propio tipo de mensaje**: `MotorCommand`.

## 1. Crea tu proyecto

```
mi_robot/
├── agmen/               ← copia aquí la carpeta de Agmen (o: git clone ... agmen)
├── CMakeLists.txt
├── motor_command.agmen  ← tu mensaje (desde Agmen 0.7)
├── motor.cpp
└── controlador.cpp
```

Los archivos están completos en [`examples/tutorial/`](../../examples/tutorial/).
Cópialos de ahí; abajo se explican las partes importantes.

`CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.20)
project(mi_robot LANGUAGES CXX)

add_subdirectory(agmen)          # la carpeta donde está Agmen

# Tu mensaje: de motor_command.agmen sale motor_command.hpp (capítulo 11)
agmen_add_messages(mi_robot_msgs motor_command.agmen)

add_executable(motor motor.cpp)
target_link_libraries(motor PRIVATE mi_robot_msgs)

add_executable(controlador controlador.cpp)
target_link_libraries(controlador PRIVATE mi_robot_msgs)
```

`mi_robot_msgs` trae tus mensajes y `agmen::core`: es todo lo que hace falta
para usar Agmen. (Si no tienes mensajes propios, enlaza directo con
`agmen::core`.)

## 2. Tu tipo de mensaje: `motor_command.agmen`

```text
# Velocidad deseada de cada rueda, de -1 (reversa total) a +1 (adelante total).
message MotorCommand {
    f32 left     # rueda izquierda
    f32 right    # rueda derecha
}
```

Eso es todo lo que escribes. Al compilar, CMake corre `agmen gen` y crea
`motor_command.hpp`, que tus nodos incluyen con `#include "motor_command.hpp"`.
Vale la pena ver una vez qué hay adentro (está en
`build/agmen_gen/mi_robot_msgs/`), porque eso es lo que hace funcionar a
cualquier mensaje de Agmen:

```cpp
struct MotorCommand {
    static constexpr std::string_view kName = "MotorCommand";
    static constexpr std::string_view kSchema = "MotorCommand{f32 left;f32 right}";
    static constexpr std::size_t kPayloadLen = 8;

    float left{};   ///< rueda izquierda
    float right{};  ///< rueda derecha

    void encode_payload(agmen::wire::Bytes& out) const;          // left y luego right, 4 bytes cada uno
    static MotorCommand decode_payload(agmen::wire::ByteView p); // lo inverso; valida que midan 8 bytes
};

static_assert(agmen::wire::Message<MotorCommand>);
static_assert(agmen::wire::compact_size_v<MotorCommand> == MotorCommand::kPayloadLen);
```

| Parte | Para qué |
|---|---|
| `kName` | Nombre legible del tipo (aparece en los errores) |
| `kSchema` | La "receta" del mensaje. Agmen calcula su `schema_id` y su tamaño **al compilar**. Si cambias un campo, cambia el `schema_id` y los nodos con la versión vieja lo detectan |
| `encode_payload` | Convierte tus datos a bytes (little-endian, el layout de un struct de C) |
| `decode_payload` | Lo inverso; valida el tamaño |
| `static_assert(...)` | El compilador revisa que el struct cumpla todo lo que Agmen necesita. Si algo no cuadra, no compila (mejor eso que un error en el robot) |

💡 Hasta Agmen 0.6 este struct se escribía a mano, y todavía se puede (así
está `ImuSample` en `include/agmen/wire.hpp`). Con el `.agmen` no te puedes
equivocar en el orden de los bytes, y el mismo archivo sirve para Python
(capítulo 3) y para tipos mucho más grandes (capítulo 11).

## 3. El nodo `motor`

Las partes clave de `motor.cpp`:

```cpp
agmen::Subscriber<MotorCommand> sub("127.0.0.1:7500");     // 1. escuchar

if (auto sample = sub.receive(milliseconds(50))) {         // 2. esperar (máx 50 ms) y validar
    MotorCommand cmd = sample->message;                    // 3. el mensaje ya decodificado
    cmd.left = std::clamp(cmd.left, -1.0f, 1.0f);          // 4. nunca confiar a ciegas
    ...
}

if (!stopped && steady_clock::now() - last_cmd > kSafetyTimeout) {  // 5. freno de seguridad
    current = MotorCommand{};   // velocidad 0
}
```

1. **Escuchar** en un puerto.
2. **Esperar** un mensaje, pero máximo 50 ms. Así el nodo nunca se congela y
   puede revisar el freno de seguridad aunque no llegue nada.
3. **Decodificar.** `receive` ya lo hizo. Si llega basura o un mensaje de
   otro tipo, lanza `WireError` con una explicación clara. En
   `sample->envelope` tienes el sobre: `seq`, `t_data`, `t_pub`, `source_id`.
4. **Recortar** los valores a un rango válido. Un bug en otro nodo no debe
   poder mandar tu motor al 300 %.
5. **Freno de seguridad** (*deadman*): si pasan 0.5 s sin órdenes, alto
   total. Es la regla más importante de cualquier robot que se mueve.

## 4. El nodo `controlador`

```cpp
agmen::Publisher<MotorCommand> pub("127.0.0.1:7500", "/cmd");   // destino y topic

for (long seq = 0; ...; ++seq) {
    const MotorCommand cmd{.left = 0.5f + 0.3f * std::sin(t),
                           .right = 0.5f - 0.3f * std::sin(t)};
    pub.publish(cmd);
    agmen::platform::sleep_until(start + kPeriod * (seq + 1));   // 50 Hz exactos
}
```

El `Publisher` lleva la cuenta de `seq`, pone las marcas de tiempo y una vez
por segundo **anuncia** el esquema, para que el panel y `agmen echo` puedan
mostrar tus comandos sin saber qué es un `MotorCommand`. Si los datos
corresponden a otro instante (por ejemplo, cuándo leyó el sensor), pásalo:
`pub.publish(cmd, t_lectura_ns)`.

💡 **¿Por qué `sleep_until` y no `sleep(20 ms)`?** Si duermes 20 ms *después*
de trabajar, el tiempo de trabajo se va sumando y terminas a 48 Hz en vez de
50. Con `sleep_until(inicio + k·periodo)` cada mensaje sale en su momento
exacto. Además, `agmen::platform::sleep_until` es preciso también en Windows,
donde el sleep normal tiene ~15 ms de error.

## 5. Compila y corre

```bash
cd mi_robot
cmake -S . -B build
cmake --build build --config Release
```

Terminal 1:
```bash
./build/motor          # Windows: build\Release\motor.exe
```

Terminal 2:
```bash
./build/controlador    # Windows: build\Release\controlador.exe
```

En la terminal del motor:

```
[motor] Agmen 0.1.0 · esperando MotorCommand en 127.0.0.1:7500
[motor] ✅ recibiendo comandos, ruedas activas
[motor] rueda izq=+0.64  der=+0.36
[motor] rueda izq=+0.79  der=+0.21
```

💡 ¿Solo quieres probar sin crear el proyecto? Los mismos nodos ya vienen
compilados en Agmen: `./build/bin/tutorial_motor` y
`./build/bin/tutorial_controlador`.

## 6. Experimentos

🧪 **Prueba el freno de seguridad:** con el motor corriendo, detén el
controlador (Ctrl+C). En medio segundo:
```
[motor] ⛔ ALTO: sin comandos desde hace 0.5 s (timeout de seguridad)
```

🧪 **Cambia el comportamiento:** en `controlador.cpp`, haz que el robot gire
en su lugar (`left = 0.5`, `right = -0.5`). Recompila con
`cmake --build build`.

🧪 **Manda órdenes inválidas:** pon `left = 5.0f`. ¿Qué imprime el motor? ¿Por qué?

## Lo que aprendiste

- A usar Agmen en **tu propio proyecto** con `add_subdirectory`, y a definir tu mensaje en un `.agmen`.
- A definir **tu propio tipo de mensaje** y que el compilador lo valide.
- Dos reglas de oro: **no confíes en los datos que llegan** y **frena si te
  quedas sin órdenes**.

**Siguiente:** [Capítulo 3 — Un cerebro en Python](03-python.md)
