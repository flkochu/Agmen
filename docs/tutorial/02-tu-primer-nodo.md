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
├── motor_command.hpp
├── motor.cpp
└── controlador.cpp
```

Los tres archivos `.hpp`/`.cpp` están completos en
[`examples/tutorial/`](../../examples/tutorial/). Cópialos de ahí; abajo se
explican las partes importantes.

`CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.20)
project(mi_robot LANGUAGES CXX)

add_subdirectory(agmen)          # la carpeta donde está Agmen

add_executable(motor motor.cpp)
target_link_libraries(motor PRIVATE agmen::core)

add_executable(controlador controlador.cpp)
target_link_libraries(controlador PRIVATE agmen::core)
```

`target_link_libraries(... agmen::core)` es todo lo que hace falta para usar
Agmen. Así de simple.

## 2. Tu tipo de mensaje: `motor_command.hpp`

```cpp
struct MotorCommand {
    static constexpr std::string_view kName = "MotorCommand";
    static constexpr std::string_view kSchema = "MotorCommand{f32 left;f32 right}";

    float left = 0.0f;    // -1 (reversa) ... +1 (adelante)
    float right = 0.0f;

    void encode_payload(agmen::wire::Bytes& out) const {
        agmen::wire::put_f32(out, left);
        agmen::wire::put_f32(out, right);
    }
    static MotorCommand decode_payload(agmen::wire::ByteView p) { /* lo inverso */ }
};

static_assert(agmen::wire::Message<MotorCommand>);
```

| Parte | Para qué |
|---|---|
| `kName` | Nombre legible del tipo (aparece en los errores) |
| `kSchema` | La "receta" del mensaje. Agmen calcula su `type_id` **al compilar**. Si cambias un campo, cambia el `type_id` y los nodos con la versión vieja lo detectan |
| `encode_payload` | Convierte tus datos a bytes (little-endian, el formato de Agmen) |
| `decode_payload` | Lo inverso; valida el tamaño |
| `static_assert(...)` | El compilador revisa que tu struct cumpla todo lo que Agmen necesita. Si te falta algo, no compila (mejor eso que un error en el robot) |

💡 En Agmen 0.5 una herramienta generará este archivo por ti a partir del
esquema. Hacerlo a mano una vez te enseña qué pasa por dentro.

## 3. El nodo `motor`

Las partes clave de `motor.cpp`:

```cpp
auto socket = agmen::net::UdpSocket::bind("127.0.0.1:7500");    // 1. escuchar

if (const auto rx = socket.recv_from(buf, milliseconds(50))) {   // 2. esperar (máx 50 ms)
    auto [header, cmd] = wire::decode<MotorCommand>(...);        // 3. decodificar y validar
    cmd.left = std::clamp(cmd.left, -1.0f, 1.0f);                // 4. nunca confiar a ciegas
    ...
}

if (!stopped && steady_clock::now() - last_cmd > kSafetyTimeout) {  // 5. freno de seguridad
    current = MotorCommand{};   // velocidad 0
}
```

1. **Escuchar** en un puerto.
2. **Esperar** un mensaje, pero máximo 50 ms. Así el nodo nunca se congela y
   puede revisar el freno de seguridad aunque no llegue nada.
3. **Decodificar.** Si llega basura o un mensaje de otro tipo, `decode` lanza
   `WireError` con una explicación clara.
4. **Recortar** los valores a un rango válido. Un bug en otro nodo no debe
   poder mandar tu motor al 300 %.
5. **Freno de seguridad** (*deadman*): si pasan 0.5 s sin órdenes, alto
   total. Es la regla más importante de cualquier robot que se mueve.

## 4. El nodo `controlador`

```cpp
auto socket = agmen::net::UdpSocket::bind("0.0.0.0:0");   // puerto libre cualquiera
socket.connect("127.0.0.1:7500");                          // destino: el motor

for (long seq = 0; ...; ++seq) {
    const MotorCommand cmd{.left = 0.5f + 0.3f * std::sin(t),
                           .right = 0.5f - 0.3f * std::sin(t)};
    socket.send(wire::encode(cmd, seq, wire::now_ns()));
    agmen::platform::sleep_until(start + kPeriod * (seq + 1));   // 50 Hz exactos
}
```

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

- A usar Agmen en **tu propio proyecto** con `add_subdirectory` + `agmen::core`.
- A definir **tu propio tipo de mensaje** y que el compilador lo valide.
- Dos reglas de oro: **no confíes en los datos que llegan** y **frena si te
  quedas sin órdenes**.

**Siguiente:** [Capítulo 3 — Un cerebro en Python](03-python.md)
