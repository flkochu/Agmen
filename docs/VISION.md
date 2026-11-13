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
| `agmen` (comando) | `agmen run robot.toml` (todo el robot + panel), `agmen web`, `agmen mux`, `agmen echo`, `agmen hub`, `agmen topic list/info/echo/hz`, `agmen node list`, `agmen gen`, `agmen record/info/play/export` | 0.2.0 (run, web); `mux` en 0.3.0; `echo` en 0.4.0; hub y topics en 0.5.0; `--red` en 0.6.0; `gen` en 0.7.0; grabaciones en 0.12.0 |
| Archivos `.agmen` | Los mensajes de un robot en un archivo; genera C++ (`agmen_add_messages`) y Python (`agmen.load`) (RFC 0005). Desde 0.10.0 con `string`, `bytes`, `T[]` y `tensor<T,N>` (RFC 0008) | 0.7.0; tipos variables en 0.10.0 |
| `agmen.msgs` | Mensajes estándar: `Imagen` e `ImagenComprimida`, con los campos de Foxglove (RFC 0008 D4); `MuxEstado`, quién manda en un mux (RFC 0012) | 0.10.0; `MuxEstado` en 0.13.0 |
| Memoria compartida | Imágenes y tensores sin copiar entre procesos de la misma computadora; entre computadoras, TCP. Sin cambiar el código de los nodos (RFC 0009) | 0.11.0 |
| `agmen::Node` / `agmen.Node` | Varias frecuencias en un nodo (`every`, `subscribe`/`on`, colas, grupos con su propio hilo) y el reloj de Agmen (real y simulación, `/clock`) (RFC 0006) | 0.8.0 |
| Panel web | Nodos, logs, sensores, inspector, cámara y manejo desde el navegador; el robot no dibuja (RFC 0002, 0003). Mandos de cualquier tipo y quién manda en cada mux (RFC 0012) | 0.2.0; WebSocket, red con clave y Foxglove en 0.6.0; cámara en 0.10.0; mandos en 0.13.0. El puente sigue en Python: medido, no frena al robot (RFC 0008 D6) |
| `agmen` (Python, PyPI) | `pip install agmen` / `import agmen`. Desde 0.9.0 trae el núcleo de C++ compilado (`agmen._core`, nanobind) para 5 sistemas; Python puro como respaldo y referencia | 0.1.0; compilado en 0.9.0 |
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
| g | Núcleo en C++; Windows y Linux, x86-64 y ARM64 |
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
10. **Multiplataforma verificada por CI** (Windows y Linux, x86-64 y ARM64), con advertencias tratadas como errores.
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

### Sin macOS (2026-11-05, desde 0.11.1)

Agmen deja de tener soporte oficial en macOS: se prueba y se publica para
**Windows y Linux, x86-64 y ARM64**. Lo recomendó el profesor asesor.

**Por qué:**
- **El CI de macOS fallaba por tiempos, no por Agmen.** Sus máquinas virtuales
  despiertan tarde de cada `sleep` y a veces se congelan: `py_bench.py` y
  `run_smoke.py` fallaron así en el PR #1, en el release de 0.11.0 y en el PR
  #2, y cada vez hubo que volver a correrlas. Una falla que
  se ignora "porque es macOS" esconde la que sí es de verdad (regla 1).
- **Dos wheels de más** (Intel y Apple Silicon), las más lentas del release.
- **Los robots no corren en macOS.** Lo que se usa es Linux (Raspberry Pi,
  Jetson, una computadora a bordo) y Windows para desarrollar y para el panel.

**Lo que no cambia:**
- No se borra código: `src/shm.cpp` y `src/net.cpp` siguen siendo POSIX, y
  `src/platform.cpp` conserva su `#ifdef __APPLE__`. Compilar en macOS debería
  seguir funcionando, pero ya nadie lo revisa.
- En macOS, `pip install agmen` instala la wheel de **Python puro**
  (`py3-none-any`): todo funciona igual, más lento.
- Si alguien quiere volver a probar en macOS, basta agregar `macos-latest` al
  CI; la decisión se puede revisar si hay quien la mantenga.

## 5. Estado contra la visión

### Se queda igual
- La prueba de interoperabilidad (C++ ↔ Python), ahora también con vectores de prueba byte por byte.
- El hash del esquema con errores claros, los números de secuencia y el lazo por *deadline*.
- El payload de `ImuSample`: el de v0 ya era layout `compact` válido y no cambió.
- CI en Windows, Linux y Linux ARM64 (y en macOS hasta 0.11.0, §4).

### Cambió en 0.4.0 (RFC 0001, nivel 1)
- ✅ Header de 32 B → sobre de 64 B con `header_len`, `encoding`, `flags`, `topic_id`, `source_id`, `t_data`/`t_pub` y reloj.
- ✅ Payload `compact` (struct de C) definido por un esquema en texto; en Python el layout sale solo del esquema.
- ✅ Decodificación dinámica: `agmen echo` y el panel leen cualquier tipo gracias al anuncio de esquema.

### Cambió en 0.6.0 (RFC 0002, nivel 3)
- ✅ El panel habla por WebSocket y recibe los **bytes de Agmen sin convertir**: el navegador los decodifica con `agmen.js`, la tercera implementación del formato (C++, Python, JavaScript), probada con los mismos vectores.
- ✅ Modo red local con clave; hombre muerto en el puente; compatible con Foxglove (solo lectura).

### Cambió en 0.7.0 (RFC 0005, nivel 4)
- ✅ Mensajes de C++ y Python generados desde un archivo `.agmen` en vez de escritos a mano, con enums, constantes y valores por defecto. Los bytes no cambian: un tipo se identifica por su texto canónico.

### Cambió en 0.8.0 (RFC 0006, nivel 5)
- ✅ El reloj de Agmen (regla 5): `t_data`, `t_pub` y los temporizadores usan `agmen::clock`, que en simulación sigue al topic `/clock` (más rápido, más lento, en pausa o paso a paso). El campo `clock` del sobre ya se llena, y un mensaje de otro reloj se avisa.
- ✅ Dentro de un nodo ya hay paralelismo: cada `node.group(...)` es otro hilo (medido en `paralelo_check`: un control a 1 kHz no pierde ciclos junto a una tarea de 80 ms en otro grupo).

### Cambió en 0.9.0 (RFC 0007, nivel 6)
- ✅ El paquete de Python usa el núcleo de C++ (`agmen._core`): formato, topics, `Node` y reloj son el mismo código que en C++. Codificar ~5× y publicar ~4.5× más rápido; el `Node` de Python espera sin el GIL. NumPy sin copias con `agmen.load(..., numpy=True)`.
- ✅ `pip install agmen` no compila nada en Windows, Linux x64 y ARM64 (wheels con cibuildwheel; también en macOS Intel y Apple Silicon hasta 0.11.0); en lo demás, Python puro.

### Cambió en 0.10.0 (RFC 0008, nivel 7)
- ✅ Codificación `arrow` (RFC 0001 §6) implementada en C++, Python y JavaScript con los mismos bytes; `pyarrow` la valida en el CI. Un mensaje con `string`, `bytes`, `T[]` o `tensor<T,N>` va en arrow y por **TCP** (hasta 64 MiB); los demás siguen en `compact` por UDP sin cambiar un byte.
- ✅ Un suscriptor lento recibe solo el mensaje más nuevo y no frena al publicador (medido: una cámara de 640×480 a 30 fps, 27 MB/s, con un navegador que no lee, y el control a 1 kHz no pierde ciclos).
- ✅ `agmen.msgs.Imagen` e `ImagenComprimida`; la tarjeta Cámara del panel; Foxglove las ve como `foxglove.RawImage` / `CompressedImage`.

### Cambió en 0.11.0 (RFC 0009, nivel 8)
- ✅ Requisito a, sin copias: en la misma computadora, un mensaje `arrow` de 4 KiB o más se escribe **una vez** en memoria compartida (la bandera `SHM` del sobre, reservada desde el RFC 0001) y por TCP viaja solo un descriptor de 72 bytes. En Python llega como `memoryview` / `numpy.ndarray` que apunta a la memoria del publicador. Medido (`bench/shm_vs_tcp.py`): 1920×1080 con 3 suscriptores, de 5.1 ms a 1.9 ms de latencia; 4K con 3 suscriptores, de 21.9 ms a 5.8 ms, y la cámara pasa de 28.9 ms a 2.6 ms de CPU por imagen (con TCP paga una copia por suscriptor).
- ✅ Propia y sin dependencias (D1: no iceoryx2), en C++ y en Python puro con los mismos bytes; Linux y Windows (y macOS hasta 0.11.0). El publicador es el dueño de los bloques y el suscriptor se los devuelve por la conexión TCP al soltarlos: un proceso que muere no deja candados, y si es el suscriptor, sus bloques vuelven en el acto.
- ✅ Misma computadora se **prueba**, no se adivina (D4): si el suscriptor no puede abrir la memoria del publicador (otra computadora, otro contenedor, otro usuario), todo sigue por TCP y se avisa una vez. Con dirección fija, el suscriptor escucha TCP en la IP que pide: las imágenes ya cruzan la red.
- ✅ Nadie frena a nadie y la memoria tiene tope (D6): hasta 4 mensajes (o 64 MiB) prestados por suscriptor; hasta 8 (o 128 MiB) sin devolver por conexión, y luego D3 del RFC 0008; un pool por publicador de hasta 512 MiB, ¼ de la RAM o ½ de `/dev/shm` (lo menor; 0.11.1), que se encoge solo: un bloque sin usar 5 s se libera. Los bloques de un nodo que se cayó los borra el siguiente que arranca.

### Cambió en 0.12.0 (RFC 0010, nivel 9)
- ✅ Requisito c, grabar y reproducir: `agmen record` (un suscriptor más, por memoria compartida cuando puede) guarda los topics en **MCAP** con el paquete de Agmen completo adentro, sin cambiar un byte; nunca frena a nadie (cola con tope; las pérdidas se cuentan y quedan escritas) y, si se cae, pierde como mucho el último segundo. `agmen run --record`, `[record]` y el botón ⏺ del panel.
- ✅ `agmen play` y `agmen run --play` reproducen con **el tiempo de la grabación** (el reloj `replay` del RFC 0006, en C++ y Python): los nodos no notan la diferencia, y `--skip` sustituye a los sensores (como dora). El panel lee el `.mcap` en el navegador, por partes, con barra de tiempo; `agmen export --foxglove` lo prepara para Foxglove.
- ✅ Datasets: `agmen.Recording` (mensajes como `Sample`, tablas de pyarrow con el mapeo canónico) y `agmen export --parquet`. Tres lectores de MCAP (Python, JavaScript y la biblioteca oficial) leen igual el mismo vector de prueba.

### Cambió en 0.13.0 (RFC 0012, para Arca9)
- ✅ El panel manda **cualquier tipo `compact`** (`[[web.command]]`): botones con un mensaje fijo y deslizadores y casillas armados con el esquema, con hombre muerto (manda solo mientras se mantiene presionado; al soltar se calla). El servidor arma el mensaje y revisa tipos y rangos: nunca reenvía bytes del navegador. Arca9 ya no necesita un nodo que traduzca `MotorCommand`.
- ✅ El mux publica **quién manda** en `/mux/<nombre>` (`agmen.msgs.MuxEstado`), y el panel lo muestra y avisa cuando la entrada que mandaba se calla. `[[topic]] max_gap_ms` avisa cuando un topic deja de llegar.

### Falta (siguientes niveles)
- El formato LeRobot (Parquet + MP4, por episodios), encima de esto (nivel 13).
- Que la cámara escriba directo en la memoria compartida (préstamo para escribir, RFC 0009 §4): quitaría la copia que queda en el publicador.

### Se rehace
- **Python**: ✅ de reimplementación en Python puro a paquete compilado desde el núcleo de C++ (nanobind, 0.9.0). La versión pura se queda como referencia y para pruebas.
- **Transporte**: ✅ de UDP punto a punto a pub/sub con topics (0.5.0, el hub es solo de control), ✅ TCP para los mensajes grandes (0.10.0) y ✅ memoria compartida local (0.11.0). Falta: red entre máquinas con topics (nivel 11).

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
| Memoria compartida | Arriba de 4 KB; los *drop tokens* vuelven por el daemon | Arriba de 4 KiB (`arrow`); el bloque se devuelve directo por la conexión TCP del suscriptor, sin daemon; si no se puede abrir, TCP (RFC 0009) |
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
| **2 ✅** (0.5.0) | **Pub/sub local: topics con `agmen hub` (RFC 0004), `agmen node list`, `agmen topic list/info/echo/hz`, topics esperados en robot.toml; el panel muestra cualquier topic** | i |
| **3 ✅** (0.6.0) | **Panel por WebSocket (RFC 0002): el navegador decodifica los bytes de Agmen, varias gráficas a la vez, inspector de mensajes, modo red local con clave, hombre muerto en el puente, compatible con Foxglove** | h |
| **4 ✅** (0.7.0) | **Archivos `.agmen` (RFC 0005): mensajes con enums, constantes y valores por defecto; `agmen gen` genera C++ y Python, `agmen_add_messages` en CMake, `agmen.load` en Python** | b |
| **5 ✅** (0.8.0) | **Executor y tiempo (RFC 0006): `Node` con `every`, `subscribe`/`on`, colas (`queue = 1` el más reciente), grupos con su propio hilo, ciclos saltados contados y avisados; reloj de Agmen real/sim con `/clock`, pausa, paso y velocidad desde el panel** | d, e |
| 6 ✅ (0.9.0) | Python de primera clase (RFC 0007): `agmen._core` compilado con nanobind (formato, topics, `Node`, reloj), wheels para 5 sistemas y Python puro de respaldo, NumPy sin copias | b |
| **7 ✅** (0.10.0) | **Datos grandes (RFC 0008): codificación `arrow` (`string`, `bytes`, `T[]`, `tensor<T,N>`) por TCP con descarte por suscriptor lento; `agmen.msgs.Imagen`; cámara en el panel y en Foxglove. El puente del panel se quedó en Python: medido, cumple** | a, b, h |
| **8 ✅** (0.11.0) | **Memoria compartida (RFC 0009): imágenes y tensores sin copiar entre procesos de la misma computadora, en C++ y Python, Linux y Windows (y macOS hasta 0.11.0); se prueba si se puede y si no, TCP; préstamos que se devuelven solos y con tope; `bench/shm_vs_tcp.py`** | a, b, g |
| **9 ✅** (0.12.0) | **Grabar y reproducir (RFC 0010): `agmen record` en MCAP sin frenar a nadie, `agmen play` con el reloj `replay` y sustitución de nodos, el `.mcap` en el panel con barra de tiempo y en Foxglove, `agmen.Recording` y `agmen export --parquet`; `bench/grabar.py`** | c |
| 10 | Abstracción sim/real; `robot.toml` con nodos en varias máquinas (el lanzador básico ya existe desde 0.2.0) ([RFC 0011](rfc/0011-sim-real-y-varias-computadoras.md), aceptado el 2026-11-13) | d, i |
| — (propuesto) | Mensajes estándar y marcos de referencia: la base que necesitan el visualizador, el control, el puente genérico de MuJoCo y el puente con ROS 2 ([RFC 0013](rfc/0013-mensajes-estandar-y-marcos.md)). Propuesto, sin decidir; su lugar en el roadmap es la decisión D1 | a, c, d, f |
| 11 | Red multi-máquina, sincronización de relojes y **`agmen-web` en la nube** (relay con autenticación) | c, h |
| 12 | Puentes (`agmen-iot` MQTT, ROS 2) y `agmen-micro` | f |
| 13 | Paquetes de IA: `agmen-vision`, `agmen-learn` | — |
| 14 | **Visualizador propio** ("nuestro Foxglove", pedido por Adán para cuando Agmen esté completo): 3D, URDF, nubes de puntos, layouts guardados, reproducir grabaciones; sobre `agmen.ws` y `agmen.js` | h |

## 8. Decisiones pendientes

| Decisión | Cuándo |
|---|---|
| ~~¿Aprobar el formato del RFC 0001?~~ | ✅ Aceptado el 2026-10-30 |
| ~~¿Aprobar el diseño de la interfaz web (RFC 0002)?~~ | ✅ Aceptado el 2026-11-03: bytes de Agmen al navegador, puente en Python (C++ en el nivel 7), clave en red, Foxglove solo lectura |
| ¿Aprobar el lanzador y el panel (RFC 0003)? | Ya implementado como prototipo; se puede ajustar |
| ~~Grafo estático declarado (como dora) o pub/sub dinámico (como ROS)~~ | ✅ Dinámico + topics esperados en robot.toml ([RFC 0004](rfc/0004-topics-y-descubrimiento.md), 2026-11-02) |
| ~~Daemon local obligatorio o sin daemon~~ | ✅ Hub local de control, datos directos ([RFC 0004](rfc/0004-topics-y-descubrimiento.md), 2026-11-02) |
| ~~Compatibilidad con el protocolo WebSocket de Foxglove~~ | ✅ Solo lectura en 0.6.0 (RFC 0002 §5.3). Publicar desde Foxglove: después |
| ~~Archivos `.agmen`: sintaxis, quién genera, qué trae~~ | ✅ RFC 0005 aceptado el 2026-11-03 (las cinco opciones recomendadas) |
| ~~Executor: un hilo o varios; tiempo de simulación~~ | ✅ RFC 0006 aceptado el 2026-11-03: un hilo por defecto y grupos con su propio hilo; un reloj para todo el robot en robot.toml |
| ~~Python: qué pasa a C++, wheels, NumPy~~ | ✅ RFC 0007 aceptado el 2026-11-04 (las seis opciones recomendadas) |
| ~~Datos grandes: implementar Arrow, TCP o UDP, imágenes estándar, cámara en el panel~~ | ✅ RFC 0008 aceptado el 2026-11-04 (las seis opciones recomendadas) |
| Prioridades de tiempo real (`SCHED_FIFO`, afinidad de núcleo) por grupo | Cuando un robot lo necesite |
| Unidades y rangos en los esquemas, que muestre el panel | Cuando se diseñe (cambia el anuncio del RFC 0001) |
| Nombre y alcance del visualizador propio | Nivel 14 |
| ~~Memoria compartida propia o con iceoryx2 por debajo~~ | ✅ [RFC 0009](rfc/0009-memoria-compartida.md) aceptado el 2026-11-05: propia (D1); D1–D5 y D7 = a, D6 = a′ (tope según la RAM y el pool se encoge, en 0.11.1) |
| Préstamo para escribir directo en la memoria compartida (la cámara llena el bloque) | Cuando se mida que la copia del publicador estorba (RFC 0009 §4) |
| ~~¿Seguir probando y publicando para macOS?~~ | ✅ No, desde 0.11.1 (2026-11-05, §4): Windows y Linux, x86-64 y ARM64; en macOS, Python puro |
| ~~Formato de grabación: MCAP o propio sobre Arrow IPC~~ | ✅ [RFC 0010](rfc/0010-grabar-y-reproducir.md) aceptado el 2026-11-05 (D1–D9 = a): MCAP con los mensajes de Agmen adentro, sin cambiar un byte |
| ~~Simulación y real; nodos y topics en varias computadoras~~ | ✅ [RFC 0011](rfc/0011-sim-real-y-varias-computadoras.md) aceptado el 2026-11-13 (D1–D10 = a): perfiles en `robot.toml`, un mundo de MuJoCo y `agmen agent` en cada computadora; sin implementar, se hace en 0.14.0 |
| ~~Mandos del panel de cualquier tipo; quién manda en el mux; `max_gap_ms`~~ | ✅ [RFC 0012](rfc/0012-mandos-del-panel-y-quien-manda.md) aceptado el 2026-11-13 (D1–D6 = a), implementado en 0.13.0 a pedido de Arca9 |
| Mensajes estándar y marcos de referencia (articulaciones, pose, velocidad, odometría, IMU, láser, nube de puntos, mapa, trayectorias y el equivalente a TF: qué va en `compact` y qué en `arrow`, dónde van los nombres y el orden de las articulaciones, cómo se publican y se consultan los marcos) | [RFC 0013](rfc/0013-mensajes-estandar-y-marcos.md), **propuesto** el 2026-11-13; faltan D1–D14 |

## Fuentes

- dora-rs: <https://github.com/dora-rs/dora> y el código de `dora-node-api` (umbral de 4096 bytes)
- Extensiones canónicas de Arrow (tensores): <https://arrow.apache.org/docs/format/CanonicalExtensions.html>
- Arrow PyCapsule Interface: <https://arrow.apache.org/docs/python/extending_types.html>
- iceoryx2: <https://pypi.org/project/iceoryx2/>
- Formato LeRobot v3 (Parquet + MP4): <https://huggingface.co/docs/lerobot/porting_datasets_v3>
