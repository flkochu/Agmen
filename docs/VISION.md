# Motio — Visión, estado y roadmap

> *Motio* (latín): movimiento.

## 1. Qué es Motio

Motio es el **sistema nervioso** de un robot. Mueve mensajes entre nodos de
forma rápida, confiable y con marca de tiempo, y no hace nada más. Sirve para
robótica, IA (visión por computadora, aprendizaje por refuerzo, políticas que
mueven un robot) e IoT. Está pensado para que lo use **una comunidad**, no
solo su autor.

**Principio central: Motio no lleva la IA adentro.** El núcleo no sabe qué es
una imagen, una red neuronal o un broker MQTT. Todo eso vive en paquetes
opcionales que se conectan al núcleo:

```
  motio-web        motio-vision   motio-learn   motio-iot   (simuladores, drivers, tus nodos)
  (navegador)            │              │            │              │
       │     ┌───────────┴──────────────┴────────────┴──────────────┴──────────┐
       └─────┤ motio-core (C++20): mensajes, esquemas, transporte, tiempo,     │
             │ grabación                                                       │
             └─────────────────────────────────────────────────────────────────┘
                 C++ · Python (de primera clase) · JavaScript · motio-micro (MCU)
```

| Paquete | Qué es | Cuándo |
|---|---|---|
| `motio-core` | Núcleo en C++20: `#include <motio/motio.hpp>`, `namespace motio::` | Desde ya |
| `motio-web` | Interfaz web: el navegador dibuja, el robot solo publica (RFC 0002) | Prototipo ya; versión C++ en el nivel 3 |
| `motio` (CLI) | `motio run`, `motio node list`, `motio topic echo /imu` | Nivel 2 |
| `motio` (Python) | `import motio`, compilado desde el núcleo de C++ con nanobind | Nivel 6 |
| `motio-micro` | Cliente ligero para microcontroladores | Nivel 12 |
| `motio-iot` | Puentes MQTT y similares | Nivel 12 |
| `motio-vision`, `motio-learn` | Nodos y utilidades de IA | Nivel 13 |

## 2. Requisitos del núcleo

No se implementan todos ya, pero ninguna decisión debe impedirlos.

| | Requisito |
|---|---|
| a | Datos grandes (imágenes, tensores, nubes de puntos) sin copias entre nodos |
| b | Python de primera clase (ahí viven PyTorch, YOLO...), C++ nativo |
| c | Todo mensaje con marca de tiempo; las grabaciones sirven como datasets |
| d | Misma interfaz para simulación y robot real |
| e | Nodos a frecuencias distintas sin bloquearse (IA a 10 Hz, motores a 1 kHz) |
| f | IoT por puentes, fuera del núcleo; a futuro, versión para microcontroladores |
| g | Núcleo en C++; Windows, Linux y macOS |
| h | **Interfaz gráfica que no le cueste recursos al robot**: web, local, red o nube (RFC 0002) |

## 3. Reglas de diseño

1. **Nunca fallar en silencio.** Toda incompatibilidad se reporta con un error claro.
2. **El núcleo solo mueve mensajes.** Si algo puede vivir en un paquete aparte, vive aparte.
3. **El formato es el contrato.** Cualquier lenguaje que lo implemente es un nodo válido.
4. **Esquema obligatorio.** No hay topics sin tipo.
5. **El tiempo es de primera clase.** Los nodos usan el reloj de Motio, nunca el del sistema operativo.
6. **Sin copias para datos grandes; sin sobrecosto para datos chicos.**
7. **El robot no dibuja.** La interfaz vive en el navegador.
8. **Discovery que funcione en cualquier red**, sin depender de multicast.
9. **Cero boilerplate y fácil de compilar**, para que la comunidad pueda contribuir.
10. **Multiplataforma verificada por CI**, con advertencias tratadas como errores.

## 4. Decisión: núcleo en C++ (2026-10-30)

El nivel 0 se escribió primero en Rust y se migró a C++20 por indicación del
profesor asesor. El objetivo es que Motio lo use una comunidad.

**Por qué C++:**
- Es el lenguaje que la comunidad de robótica ya conoce: ROS 2, Arduino,
  PX4, OpenCV y PCL están en C++. Más gente puede usar y contribuir desde el
  día uno.
- Los usuarios de C++ lo integran de forma nativa, sin una capa de bindings.
- Los microcontroladores (ESP32, STM32) se programan en C/C++.

**Lo que cuesta, y cómo se mitiga:**

| Costo | Mitigación en Motio |
|---|---|
| Errores de memoria que el compilador no atrapa | AddressSanitizer + UBSan en el CI; `-Wall -Wextra -Wconversion` como errores; pruebas |
| Cada sistema operativo se comporta distinto | `motio/net.hpp` y `motio/platform.hpp` concentran las diferencias. Ya aparecieron tres: la longitud en `send` (`int` en Windows, `size_t` en POSIX), la resolución del sleep en Windows (~15.6 ms por defecto) y el `ConnectionReset` de UDP en Windows |
| Compilar y empaquetar es más difícil que en Rust | CMake estándar; sin dependencias externas en el núcleo por ahora; vcpkg/Conan cuando haga falta |

**Lo que no cambió:** el formato de mensajes. Los nodos de Python funcionaron
igual antes y después de la migración, sin tocarlos. Es la regla 3 en acción.

## 5. Estado contra la visión

### Se queda igual
- El formato v0 y su prueba de interoperabilidad (C++ ↔ Python).
- El hash del esquema con errores claros, los números de secuencia y el lazo por *deadline*.
- El payload de `ImuSample`, que ya es layout `compact` válido (RFC 0001).
- CI en Windows, Linux y macOS.

### Cambia (RFC 0001, nivel 1)
- Header de 32 B → sobre de 64 B con `header_len`, `encoding`, `flags`, `topic_id`, `source_id`, `t_data`/`t_pub` y reloj.
- Payload a mano → `compact` (struct de C) + `arrow` (datos variables y grandes).
- Mensajes a mano → generados desde un esquema (nivel 4).

### Se rehace
- **Python**: de reimplementación en Python puro a paquete compilado desde el núcleo de C++ (nanobind). La versión pura se queda como referencia y para pruebas.
- **Transporte**: UDP punto a punto era didáctico → pub/sub con topics; memoria compartida local; red después.

## 6. dora-rs como referencia

dora-rs (github.com/dora-rs/dora) es un framework en Rust para robótica e IA
que llegó a la versión 1.0 en septiembre de 2026.

### Lo que hace bien
- Arrow de punta a punta y memoria compartida para mensajes de más de 4 KB.
- Python de primera clase (paquete `dora-rs` en PyPI), además de Rust, C y C++.
- Dataflow declarativo en YAML, con colas por entrada (`queue_size`,
  `drop_oldest` o `backpressure`) y timers integrados.
- Grabar y reproducir con sustitución de nodos (pruebas de regresión).
- Multi-máquina con Zenoh, políticas de reinicio y monitoreo de salud.

### Dónde Motio puede ser distinto

| Tema | dora-rs | Motio |
|---|---|---|
| Lenguaje del núcleo | Rust | C++, el lenguaje que ya usa la comunidad de robótica |
| Tipos | Anotaciones opcionales; hash de esquema agregado en 1.0 | Esquema obligatorio desde el día 0 y decodificación dinámica |
| Mensajes chicos | Arrow siempre; debajo de 4 KB pasan por el daemon | Codificación `compact` sin sobrecosto (meta: control a 1 kHz; hay que medirlo) |
| Windows | "Best effort" | De primera clase, verificado por CI |
| Interfaz | Herramientas de terminal | Interfaz web integrada (local, red, nube); el robot no dibuja |
| Microcontroladores | Su README no los menciona | `compact` + sobre ligero (`motio-micro`) |
| Tiempo | Su README no describe la semántica | `t_data` vs `t_pub` y reloj real/sim/replay explícitos |
| Datasets | Grabaciones `.drec` | Mapeo canónico a Arrow → exportar a Parquet (formato de LeRobot) |

Siendo realistas: dora tiene un equipo y años de trabajo. La ventaja de Motio
tiene que venir de **enfocarse** (formato eficiente en ambos extremos, tiempo
bien definido, interfaz web, microcontroladores, C++ accesible) y de
apoyarse en piezas existentes donde no aporte nada reinventarlas (Zenoh,
iceoryx2).

## 7. Roadmap por niveles

| Nivel | Tema | Requisitos |
|---|---|---|
| 0 ✅ | Fundamentos en C++: serialización, UDP, seq, latencia, CI + **prototipo de interfaz web** | g, h |
| **1** | **Sobre v1 + codificación `compact` + esquema en texto + decodificación dinámica** | c, f |
| 2 | Pub/sub local: topics, discovery, CLI `motio node list`, `motio topic list/echo/hz` | — |
| 3 | `motio-web` en C++: puente WebSocket, cualquier topic, inspector, teleoperación segura, modo red local | h |
| 4 | Lenguaje de esquemas `.motio` + generador de código C++/Python | b |
| 5 | Executor y tiempo: nodos multi-frecuencia, colas por suscripción, reloj de Motio (real/sim) | d, e |
| 6 | Python de primera clase: paquete `motio` compilado (nanobind); numpy sin copias | b |
| 7 | Codificación `arrow`: imágenes, tensores; panel de cámara en la web | a, b, h |
| 8 | Memoria compartida para mensajes grandes | a |
| 9 | Grabación y reproducción; exportar a Parquet; reproducir en la web | c |
| 10 | Launch/config con un solo archivo; abstracción sim/real | d |
| 11 | Red multi-máquina, sincronización de relojes y **`motio-web` en la nube** (relay con autenticación) | c, h |
| 12 | Puentes (`motio-iot` MQTT, ROS 2) y `motio-micro` | f |
| 13 | Paquetes de IA: `motio-vision`, `motio-learn` | — |

## 8. Decisiones pendientes

| Decisión | Cuándo |
|---|---|
| ¿Aprobar el formato del RFC 0001? | **Ahora** |
| ¿Aprobar el diseño de la interfaz web (RFC 0002)? | **Ahora** |
| Grafo estático declarado (como dora) o pub/sub dinámico (como ROS) | Nivel 2 |
| Daemon local obligatorio o sin daemon | Nivel 2 |
| Compatibilidad con el protocolo WebSocket de Foxglove | Nivel 3 |
| Memoria compartida propia o con iceoryx2 por debajo | Nivel 8 |
| Formato de grabación: MCAP o propio sobre Arrow IPC | Nivel 9 |

## Fuentes

- dora-rs: <https://github.com/dora-rs/dora> y el código de `dora-node-api` (umbral de 4096 bytes)
- Extensiones canónicas de Arrow (tensores): <https://arrow.apache.org/docs/format/CanonicalExtensions.html>
- Arrow PyCapsule Interface: <https://arrow.apache.org/docs/python/extending_types.html>
- iceoryx2: <https://pypi.org/project/iceoryx2/>
- Formato LeRobot v3 (Parquet + MP4): <https://huggingface.co/docs/lerobot/porting_datasets_v3>
