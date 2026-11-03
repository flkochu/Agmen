# Agmen — Visión, estado y roadmap

> *Agmen* (latín): multitud en movimiento; columna en marcha.

## 1. Qué es Agmen

Agmen es el **sistema nervioso** de un robot. Mueve mensajes entre nodos de
forma rápida, confiable y con marca de tiempo, y no hace nada más. Sirve para
robótica, IA (visión por computadora, aprendizaje por refuerzo, políticas que
mueven un robot) e IoT. Está pensado para que lo use **una comunidad**, no
solo su autor.

**Principio central: Agmen no lleva la IA adentro.** El núcleo no sabe qué es
una imagen, una red neuronal o un broker MQTT. Todo eso vive en paquetes
opcionales que se conectan al núcleo:

```
  agmen-web        agmen-vision   agmen-learn   agmen-iot   (simuladores, drivers, tus nodos)
  (navegador)            │              │            │              │
       │     ┌───────────┴──────────────┴────────────┴──────────────┴──────────┐
       └─────┤ agmen-core (C++20): mensajes, esquemas, transporte, tiempo,     │
             │ grabación                                                       │
             └─────────────────────────────────────────────────────────────────┘
                 C++ · Python (de primera clase) · JavaScript · agmen-micro (MCU)
```

| Paquete | Qué es | Cuándo |
|---|---|---|
| `agmen-core` | Núcleo en C++20: `#include <agmen/agmen.hpp>`, `namespace agmen::` | Desde ya |
| `agmen` (comando) | `agmen run robot.toml` (todo el robot + panel), `agmen web`, `agmen mux`, `agmen echo`; luego `agmen topic echo /imu`, `agmen node list` | 0.2.0 (run, web); `mux` en 0.3.0; `echo` en 0.4.0; topics en 0.5.0 |
| Panel web | Nodos, logs, sensores y manejo desde el navegador; el robot no dibuja (RFC 0002, 0003) | 0.2.0 (Python); versión C++/WebSocket en 0.6.0 |
| `agmen` (Python, PyPI) | `pip install agmen` / `import agmen`. Hoy: referencia en Python puro; nivel 6: compilado desde C++ con nanobind | 0.1.0 |
| `agmen-micro` | Cliente ligero para microcontroladores | Nivel 12 |
| `agmen-iot` | Puentes MQTT y similares | Nivel 12 |
| `agmen-vision`, `agmen-learn` | Nodos y utilidades de IA | Nivel 13 |

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
| i | **Un solo comando** levanta todo el robot; se monitorea y se controla desde el navegador, sin abrir una terminal por nodo (RFC 0003) |

## 3. Reglas de diseño

1. **Nunca fallar en silencio.** Toda incompatibilidad se reporta con un error claro.
2. **El núcleo solo mueve mensajes.** Si algo puede vivir en un paquete aparte, vive aparte.
3. **El formato es el contrato.** Cualquier lenguaje que lo implemente es un nodo válido.
4. **Esquema obligatorio.** No hay topics sin tipo.
5. **El tiempo es de primera clase.** Los nodos usan el reloj de Agmen, nunca el del sistema operativo.
6. **Sin copias para datos grandes; sin sobrecosto para datos chicos.**
7. **El robot no dibuja.** La interfaz vive en el navegador.
8. **Discovery que funcione en cualquier red**, sin depender de multicast.
9. **Cero boilerplate y fácil de compilar**, para que la comunidad pueda contribuir.
10. **Multiplataforma verificada por CI**, con advertencias tratadas como errores.
11. **Un comando, una pantalla.** Si para usar algo hay que abrir otra terminal, es un bug de experiencia.
12. **Paralelo entre nodos, y medido.** Ningún recurso compartido (terminal, panel, red) puede hacer que un nodo espere a otro. `scripts/paralelo_check.py` lo comprueba en el CI.
13. **Un actuador, un dueño a la vez.** Si varias fuentes mandan al mismo actuador, un árbitro con prioridades (`[[mux]]`) decide, y nunca inventa comandos.

## 4. Decisiones del 2026-10-30

**Nombre: Agmen** (antes Motio). *Agmen* en latín es "multitud en movimiento,
columna en marcha": muchos nodos moviéndose juntos. Estado del nombre en
registros: `docs/PUBLICAR.md`.

**Versiones:** SemVer; cada entrega con funciones nuevas (normalmente un nivel) es una versión menor (`docs/VERSIONES.md`).

### Núcleo en C++

El nivel 0 se escribió primero en Rust y se migró a C++20 por indicación del
profesor asesor. El objetivo es que Agmen lo use una comunidad.

**Por qué C++:**
- Es el lenguaje que la comunidad de robótica ya conoce: ROS 2, Arduino,
  PX4, OpenCV y PCL están en C++. Más gente puede usar y contribuir desde el
  día uno.
- Los usuarios de C++ lo integran de forma nativa, sin una capa de bindings.
- Los microcontroladores (ESP32, STM32) se programan en C/C++.

**Lo que cuesta, y cómo se mitiga:**

| Costo | Mitigación en Agmen |
|---|---|
| Errores de memoria que el compilador no atrapa | AddressSanitizer + UBSan en el CI; `-Wall -Wextra -Wconversion` como errores; pruebas |
| Cada sistema operativo se comporta distinto | `agmen/net.hpp` y `agmen/platform.hpp` concentran las diferencias. Ya aparecieron tres: la longitud en `send` (`int` en Windows, `size_t` en POSIX), la resolución del sleep en Windows (~15.6 ms por defecto) y el `ConnectionReset` de UDP en Windows |
| Compilar y empaquetar es más difícil que en Rust | CMake estándar; sin dependencias externas en el núcleo por ahora; vcpkg/Conan cuando haga falta |

**Herramientas en Python, núcleo en C++.** El comando `agmen` (lanzador y
panel) está en Python, igual que el comando `ros2`: orquestar procesos no es
el camino crítico, y así es más fácil de instalar (`pip install agmen`) y de
contribuir. Lo que mueve mensajes rápido sigue en C++.

**Lo que no cambió:** el formato de mensajes. Los nodos de Python funcionaron
igual antes y después de la migración, sin tocarlos. Es la regla 3 en acción.

## 5. Estado contra la visión

### Se queda igual
- La prueba de interoperabilidad (C++ ↔ Python), ahora también con vectores de prueba byte por byte.
- El hash del esquema con errores claros, los números de secuencia y el lazo por *deadline*.
- El payload de `ImuSample`: el de v0 ya era layout `compact` válido y no cambió.
- CI en Windows, Linux y macOS.

### Cambió en 0.4.0 (RFC 0001, nivel 1)
- ✅ Header de 32 B → sobre de 64 B con `header_len`, `encoding`, `flags`, `topic_id`, `source_id`, `t_data`/`t_pub` y reloj.
- ✅ Payload `compact` (struct de C) definido por un esquema en texto; en Python el layout sale solo del esquema.
- ✅ Decodificación dinámica: `agmen echo` y el panel leen cualquier tipo gracias al anuncio de esquema.

### Falta (siguientes niveles)
- Payload `arrow` para datos variables y grandes (nivel 7).
- Mensajes de C++ generados desde el esquema en vez de escritos a mano (nivel 4).

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

### Dónde Agmen puede ser distinto

| Tema | dora-rs | Agmen |
|---|---|---|
| Lenguaje del núcleo | Rust | C++, el lenguaje que ya usa la comunidad de robótica |
| Tipos | Anotaciones opcionales; hash de esquema agregado en 1.0 | Esquema obligatorio desde el día 0 y decodificación dinámica |
| Mensajes chicos | Arrow siempre; debajo de 4 KB pasan por el daemon | Codificación `compact` sin sobrecosto (meta: control a 1 kHz; hay que medirlo) |
| Windows | "Best effort" | De primera clase, verificado por CI |
| Interfaz | Herramientas de terminal | Interfaz web integrada (local, red, nube); el robot no dibuja |
| Microcontroladores | Su README no los menciona | `compact` + sobre ligero (`agmen-micro`) |
| Tiempo | Su README no describe la semántica | `t_data` vs `t_pub` y reloj real/sim/replay explícitos |
| Datasets | Grabaciones `.drec` | Mapeo canónico a Arrow → exportar a Parquet (formato de LeRobot) |

Siendo realistas: dora tiene un equipo y años de trabajo. La ventaja de Agmen
tiene que venir de **enfocarse** (formato eficiente en ambos extremos, tiempo
bien definido, interfaz web, microcontroladores, C++ accesible) y de
apoyarse en piezas existentes donde no aporte nada reinventarlas (Zenoh,
iceoryx2).

## 7. Roadmap por niveles

| Nivel | Tema | Requisitos |
|---|---|---|
| 0 ✅ (0.1.0) | Fundamentos en C++: serialización, UDP, seq, latencia, CI + prototipo de interfaz web | g, h |
| ✅ (0.2.0) | **`agmen run robot.toml` + panel web de control** (adelanto de los niveles 3 y 10, RFC 0003) | h, i |
| ✅ (0.3.0) | ¿Quién manda? Prioridad de comandos (`[[mux]]`), paralelismo medido (`scripts/paralelo_check.py`), CI en ARM64 | d, i |
| **1 ✅** (0.4.0) | **Sobre v1 + codificación `compact` + esquema en texto + decodificación dinámica**: `Publisher`/`Subscriber`, anuncio de esquema, `agmen echo`, panel que grafica cualquier tipo, vectores de prueba compartidos C++/Python | c, f |
| 2 (0.5.0) | Pub/sub local: topics, discovery, `agmen node list`, `agmen topic list/echo/hz`; el panel muestra cualquier topic | i |
| 3 (0.6.0) | Panel en C++: puente WebSocket, inspector de mensajes, teleoperación a topics, modo red local | h |
| 4 | Lenguaje de esquemas `.agmen` + generador de código C++/Python | b |
| 5 | Executor y tiempo: nodos multi-frecuencia, colas por suscripción, reloj de Agmen (real/sim) | d, e |
| 6 | Python de primera clase: paquete `agmen` compilado (nanobind); numpy sin copias | b |
| 7 | Codificación `arrow`: imágenes, tensores; panel de cámara en la web | a, b, h |
| 8 | Memoria compartida para mensajes grandes | a |
| 9 | Grabación y reproducción; exportar a Parquet; reproducir en la web | c |
| 10 | Abstracción sim/real; `robot.toml` con nodos en varias máquinas (el lanzador básico ya existe desde 0.2.0) | d, i |
| 11 | Red multi-máquina, sincronización de relojes y **`agmen-web` en la nube** (relay con autenticación) | c, h |
| 12 | Puentes (`agmen-iot` MQTT, ROS 2) y `agmen-micro` | f |
| 13 | Paquetes de IA: `agmen-vision`, `agmen-learn` | — |

## 8. Decisiones pendientes

| Decisión | Cuándo |
|---|---|
| ~~¿Aprobar el formato del RFC 0001?~~ | ✅ Aceptado el 2026-10-30 |
| ¿Aprobar el diseño de la interfaz web (RFC 0002)? | **Ahora** |
| ¿Aprobar el lanzador y el panel (RFC 0003)? | Ya implementado como prototipo; se puede ajustar |
| Grafo estático declarado (como dora) o pub/sub dinámico (como ROS) | Nivel 2: propuesta en el [RFC 0004](rfc/0004-topics-y-descubrimiento.md) (dinámico) |
| Daemon local obligatorio o sin daemon | Nivel 2: propuesta en el [RFC 0004](rfc/0004-topics-y-descubrimiento.md) (hub local de control, datos directos) |
| Compatibilidad con el protocolo WebSocket de Foxglove | Nivel 3 |
| Memoria compartida propia o con iceoryx2 por debajo | Nivel 8 |
| Formato de grabación: MCAP o propio sobre Arrow IPC | Nivel 9 |

## Fuentes

- dora-rs: <https://github.com/dora-rs/dora> y el código de `dora-node-api` (umbral de 4096 bytes)
- Extensiones canónicas de Arrow (tensores): <https://arrow.apache.org/docs/format/CanonicalExtensions.html>
- Arrow PyCapsule Interface: <https://arrow.apache.org/docs/python/extending_types.html>
- iceoryx2: <https://pypi.org/project/iceoryx2/>
- Formato LeRobot v3 (Parquet + MP4): <https://huggingface.co/docs/lerobot/porting_datasets_v3>
