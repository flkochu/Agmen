# Motio — Visión, estado y roadmap

> *Motio* (latín): movimiento.

## 1. Qué es Motio

Motio es el **sistema nervioso** de un robot. Mueve mensajes entre nodos de
forma rápida, confiable y con marca de tiempo, y no hace nada más. Sirve para
robótica, IA (visión por computadora, aprendizaje por refuerzo, políticas que
mueven un robot) e IoT.

**Principio central: Motio no lleva la IA adentro.** El núcleo no sabe qué es
una imagen, una red neuronal o un broker MQTT. Todo eso vive en paquetes
opcionales que se conectan al núcleo:

```
            motio-vision   motio-learn   motio-iot   (simuladores, drivers, tus nodos)
                  │              │            │              │
   ┌──────────────┴──────────────┴────────────┴──────────────┴───────────┐
   │ motio-core: mensajes, esquemas, transporte, tiempo, grabación       │
   └──────────────────────────────────────────────────────────────────────┘
         Rust · Python (de primera clase) · C++ · motio-micro (MCU)
```

| Paquete | Qué es | Cuándo |
|---|---|---|
| `motio-core` | Núcleo en Rust | Desde ya |
| `motio` (CLI) | `motio run`, `motio node list`, `motio topic echo /imu` | Nivel 2 |
| `motio` (Python) | `import motio`, compilado desde Rust con PyO3 | Nivel 5 |
| `motio/motio.hpp` | API de C++, `namespace motio::` | Nivel 11 |
| `motio-micro` | Cliente ligero para microcontroladores | Nivel 12 |
| `motio-iot` | Puentes MQTT y similares | Nivel 12 |
| `motio-vision`, `motio-learn` | Nodos y utilidades de IA | Nivel 13 |

## 2. Requisitos del núcleo

No se implementan todos ya, pero ninguna decisión debe impedirlos.

| | Requisito |
|---|---|
| a | Datos grandes (imágenes, tensores, nubes de puntos) sin copias entre nodos |
| b | Python de primera clase (ahí viven PyTorch, YOLO...), C++ también |
| c | Todo mensaje con marca de tiempo; las grabaciones sirven como datasets |
| d | Misma interfaz para simulación y robot real |
| e | Nodos a frecuencias distintas sin bloquearse (IA a 10 Hz, motores a 1 kHz) |
| f | IoT por puentes, fuera del núcleo; a futuro, versión para microcontroladores |
| g | Núcleo en Rust; Windows, Linux y macOS |

## 3. Reglas de diseño

1. **Nunca fallar en silencio.** Toda incompatibilidad se reporta con un error claro.
2. **El núcleo solo mueve mensajes.** Si algo puede vivir en un paquete aparte, vive aparte.
3. **El formato es el contrato.** Cualquier lenguaje que lo implemente es un nodo válido.
4. **Esquema obligatorio.** No hay topics sin tipo.
5. **El tiempo es de primera clase.** Los nodos usan el reloj de Motio, nunca el del sistema operativo.
6. **Sin copias para datos grandes; sin sobrecosto para datos chicos.**
7. **Discovery que funcione en cualquier red**, sin depender de multicast.
8. **Cero boilerplate.**
9. **Introspección desde el día 1.**
10. **Multiplataforma verificada por CI.**

## 4. Lo que llevamos (Nivel 0) contra la nueva visión

### Se queda igual

| Qué | Por qué sirve |
|---|---|
| Núcleo en Rust, sin dependencias en el formato | Requisito g |
| CI en Windows, Linux y macOS | Requisito g; ya encontró dos bugs |
| El protocolo como contrato + implementación de referencia en Python | Regla 3; Python puro queda como oráculo de pruebas |
| `type_id` por hash del esquema + errores claros | Regla 1; pasa a llamarse `schema_id` |
| Números de secuencia | Detectar pérdidas y reinicios |
| Lazo por *deadline* en el publisher | Base del executor multi-frecuencia (requisito e) |
| Payload de `ImuSample` (6 × f32 empaquetados) | Ya es layout `compact` válido, sin cambios |
| `scripts/interop.py` y pruebas de layout | Siguen siendo la red de seguridad |

### Cambia

| Antes (v0) | Ahora (v1, RFC 0001) | Por qué |
|---|---|---|
| Header de 32 B | Sobre de 64 B con `header_len`, `encoding`, `flags`, `topic_id`, `source_id`, `clock` | Crecer sin romper; topics; varios publicadores; sim/real |
| Un `stamp_ns` (u64, "momento de envío") | `t_data` + `t_pub` (i64 ns) + dominio de reloj | Requisitos c y d; fusión de sensores |
| Payload escrito a mano | `compact` con reglas de alineación de C + `arrow` para lo variable | Requisitos a, b y f |
| Hash de un string improvisado | Texto canónico de esquema, con tipos anidados expandidos, que se distribuye | Decodificación dinámica (`echo` sin código compilado) |
| `ImuSample` escrito a mano en cada lenguaje | Generado desde el esquema (nivel 3) | Cero boilerplate |

### Se rehace

| Qué | Por qué |
|---|---|
| **Python**: reimplementación en Python puro → paquete compilado desde el núcleo de Rust (PyO3 + maturin) | Requisito b: Python de primera clase significa usar el mismo motor rápido de Rust, no una copia lenta. La versión pura se queda como referencia y para pruebas |
| **Transporte**: UDP punto a punto → pub/sub con topics; memoria compartida local; red después | Era didáctico. El código de UDP se reutiliza para el transporte de red |
| **Roadmap** | Reordenado para IA + IoT (sección 6) |

## 5. dora-rs como referencia

dora-rs (github.com/dora-rs/dora) es un framework en Rust para robótica e IA
que llegó a la versión 1.0 en septiembre de 2026. No lo copiamos, pero hay
mucho que aprender de él.

### Lo que hace bien

- **Arrow de punta a punta** y memoria compartida para mensajes de más de
  4 KB. Su latencia es casi plana de 4 KB a 4 MB.
- **Python de primera clase** (paquete `dora-rs` en PyPI), además de Rust, C
  y C++.
- **Dataflow declarativo en YAML**, con colas por entrada (`queue_size`,
  `drop_oldest` o `backpressure`) y timers integrados (`dora/timer/hz/N`).
  Esto resuelve directamente nuestro requisito (e).
- **Grabar y reproducir** (`dora record` / `dora replay`) con sustitución de
  nodos, lo que permite pruebas de regresión con datos reales.
- **Multi-máquina con Zenoh**, políticas de reinicio y monitoreo de salud.

### Dónde Motio puede ser distinto

| Tema | dora-rs | Motio |
|---|---|---|
| Tipos | Anotaciones de tipo opcionales; en 1.0 agregó hash de esquema a los metadatos | Esquema **obligatorio** desde el día 0 y decodificación dinámica en todas las herramientas |
| Mensajes chicos | Arrow siempre; por debajo de 4 KB pasan por el daemon | Codificación `compact` sin sobrecosto (meta: control a 1 kHz). *Hay que medirlo contra dora cuando exista el nivel 4* |
| Windows | "Best effort", no bloquea PRs | De primera clase, verificado por CI en cada push |
| Microcontroladores | Su README no los menciona | `compact` + sobre ligero, implementable en C sin dependencias (`motio-micro`) |
| Tiempo | Su README no describe la semántica de las marcas de tiempo (internamente usa un reloj lógico híbrido) | `t_data` vs `t_pub` y dominio de reloj (real/sim/replay) explícitos en el formato |
| Datasets | Grabaciones `.drec` propias | El mapeo canónico a Arrow hace que exportar a Parquet (el formato que usa LeRobot) sea directo |
| Arquitectura | Coordinador + daemon obligatorios | Por decidir en el nivel 2: con o sin daemon en la máquina local |

**Siendo realistas:** dora tiene un equipo y años de trabajo. La ventaja de
Motio no va a venir de hacer más cosas, sino de **enfocarse**: un formato
tipado y eficiente en ambos extremos (1 kHz y 4 MB), tiempo bien definido y
microcontroladores. Y donde no aporte nada aprender a construirlo desde cero,
conviene apoyarse en piezas existentes (Zenoh, iceoryx2) detrás de una
interfaz de Motio.

## 6. Roadmap por niveles

| Nivel | Tema | Requisitos |
|---|---|---|
| 0 ✅ | Fundamentos: serialización, UDP, seq, latencia, CI | g |
| **1** | **Sobre v1 + codificación `compact` + esquema en texto + decodificación dinámica** (todavía sobre UDP) | c, f |
| 2 | Pub/sub local real: topics, discovery en la máquina, CLI `motio node list`, `motio topic list/echo/hz` | — |
| 3 | Lenguaje de esquemas `.motio` + generador de código Rust/Python/C++ | b |
| 4 | Executor y tiempo: nodos multi-frecuencia, colas por suscripción, reloj de Motio (real/sim) | d, e |
| 5 | Python de primera clase: paquete `motio` compilado desde Rust; numpy sin copias | b |
| 6 | Codificación `arrow`: variables, imágenes, tensores; pyarrow/PyTorch sin copias | a, b |
| 7 | Memoria compartida para mensajes grandes | a |
| 8 | Grabación y reproducción; exportar a Parquet | c |
| 9 | Launch/config con un solo archivo; abstracción sim/real | d |
| 10 | Red multi-máquina y sincronización de relojes | c |
| 11 | API de C++ (`motio/motio.hpp`) | b |
| 12 | Puentes (`motio-iot` MQTT, ROS 2) y `motio-micro` | f |
| 13 | Paquetes de IA: `motio-vision`, `motio-learn` | — |

## 7. Decisiones pendientes

| Decisión | Cuándo |
|---|---|
| ¿Aprobar el formato del RFC 0001? | **Ahora** |
| Grafo estático declarado (como dora) o pub/sub dinámico (como ROS) | Nivel 2 |
| Daemon local obligatorio o sin daemon | Nivel 2 |
| Memoria compartida propia o con iceoryx2 por debajo | Nivel 7 |
| Formato de grabación: MCAP o propio sobre Arrow IPC | Nivel 8 |

## Fuentes

- dora-rs: <https://github.com/dora-rs/dora> y el código de `dora-node-api` (umbral de 4096 bytes)
- Extensiones canónicas de Arrow (tensores): <https://arrow.apache.org/docs/format/CanonicalExtensions.html>
- Arrow PyCapsule Interface: <https://arrow.apache.org/docs/python/extending_types.html>
- iceoryx2: <https://pypi.org/project/iceoryx2/>
- Formato LeRobot v3 (Parquet + MP4): <https://huggingface.co/docs/lerobot/porting_datasets_v3>
