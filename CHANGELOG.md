# Changelog

Todos los cambios importantes de Agmen se anotan aquí. Formato basado en
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según
[SemVer](https://semver.org/lang/es/). Política completa en
[`docs/VERSIONES.md`](docs/VERSIONES.md).

## [Sin publicar]

### Planeado (nivel 3, versión 0.6.0)
- Panel en C++ con WebSocket: inspector de mensajes y teleoperación a topics.

## [0.5.0] — 2026-11-03

**Nivel 2: topics.** Los nodos se encuentran solos: `Publisher("/imu")`, sin
IPs ni puertos, con cuantos receptores quieras (RFC 0004, aceptado).

### Agregado
- **`agmen hub`**: el directorio de topics de la máquina. Sabe quién publica
  y quién escucha cada topic, pero **los datos van directo** de nodo a nodo:
  si el hub se cae, siguen fluyendo, y al volver los latidos le reconstruyen
  la tabla en ~1 s. Sin multicast. Protocolo de control `agmen-hub/1`, en
  texto simple.
- **Topics en `Publisher`/`Subscriber`** (C++ y Python): si el texto empieza
  con `/`, es un topic; si no, una dirección (como en 0.4). Registro y
  latido automáticos; el primer mensaje no se pierde; cada receptor nuevo
  recibe el anuncio de esquema directo. En C++, sin hilos (`heartbeat()`
  para ciclos lentos).
- **Un topic, un tipo**: publicar otro tipo en un topic existente falla con
  un error claro que dice qué choca con qué.
- **`agmen topic list | info | echo | hz`** y **`agmen node list`**. `topic
  echo` es un receptor más: no le quita el puerto a nadie.
- **Topics esperados** en `robot.toml` (`[[topic]]` con `type` y `required`):
  el hub avisa de errores de dedo ("¿Quisiste decir '/cmd'?"), de tipos
  equivocados y de topics obligatorios sin emisor. Son avisos: el grafo
  sigue siendo dinámico.
- `agmen run` arranca el hub como el nodo `hub` (o reutiliza uno vivo);
  `agmen web` abre uno dentro de su proceso si no hay. `[hub] enabled`.
- El **panel** escucha todos los topics y deja elegir cuál graficar (por
  defecto, el de más mensajes por segundo), con estadísticas por topic.
- El **mux** acepta topics: entradas `topic = "/cmd/teleop"` y `output = "/cmd"`.
- `tutorial_motor --escuchar /cmd`; el robot del tutorial usa topics.
- `net::Address`, `UdpSocket::send_to()` y `local_port()` en C++.
- Pruebas: `python/test_hub.py` (incluye reiniciar el hub sin cortar los
  datos) y `scripts/topics_smoke.py` en el CI (C++ ↔ Python en el mismo
  topic, choque de tipos, 1000 Hz a 1 y a 5 receptores sin pérdidas).
- Tutorial: capítulo 9 (topics); capítulos 5, 6 y 8 actualizados.

### Sin cambios
- El formato de los datos (v1) es el mismo de 0.4.0: un nodo 0.4 en modo
  dirección habla con uno 0.5.

## [0.4.0] — 2026-11-02

**Nivel 1: el formato de mensajes v1** (RFC 0001). Un mensaje se describe
con una línea de texto y cualquier herramienta lo entiende sin código extra.

### ⚠ Rompe compatibilidad
- El formato de cable pasa de v0 (cabecera de 32 bytes) a **v1 (sobre de 64
  bytes)**. Los nodos de Agmen 0.1–0.3 y 0.4 no se entienden: un nodo 0.4
  que recibe un mensaje v0 lo rechaza diciendo que el emisor se actualice.
  Recompila tus nodos de C++ y actualiza el paquete de Python.
- `type_id` → `schema_id` (= FNV-1a 64 de `"agmen/1 " + kSchema`); los
  nombres viejos (`type_id_v`, `TYPE_ID`, `Header`) siguen como alias.
  `Header.stamp_ns` → `Envelope.t_data` y `Envelope.t_pub`.

### Agregado
- **Sobre v1** en C++ y Python: `header_len` para crecer sin romper,
  `encoding`, `flags`, `topic_id`, `source_id` aleatorio por instancia, dos
  marcas de tiempo (`t_data`: cuándo pasó; `t_pub`: cuándo se publicó) y
  `clock`. Validación completa del RFC 0001 §8 con errores claros.
- **Esquemas en texto** (`agmen.schema`): forma canónica, `schema_id`,
  layout `compact` como un struct de C (alineación y relleno en 0), arreglos
  `T[N]`/`T[N][M]`, structs anidados, `time`/`duration`, N filas por mensaje.
  Errores con sugerencia (`tipo 'float' no existe en Agmen: usa 'f32'`).
- En **C++**, el esquema se valida **al compilar** (`AGMEN_CHECK_SCHEMA`,
  `agmen::wire::schema::check`) y `compact_size_v<M>` da su tamaño. Si
  `encode_payload` no produce lo que mide el esquema, se detecta.
- En **Python**, basta con `SCHEMA` y los campos de un `@dataclass`:
  `encode_payload`/`decode_payload` salen solos del esquema.
- **`agmen::Publisher<M>` / `agmen::Subscriber<M>`** (C++) y
  **`agmen.Publisher` / `agmen.Subscriber`** (Python): llevan `seq`, marcas de
  tiempo, `source_id` y los anuncios de esquema por ti.
- **Anuncio de esquema** (`encoding = 3`, RFC 0001 anexo A): cada emisor
  manda el texto de su esquema antes del primer dato y una vez por segundo.
  Gracias a eso, `TIPO INCOMPATIBLE` dice exactamente qué esquema llegó.
- **`agmen echo IP:PUERTO`**: muestra cualquier mensaje en la terminal (estilo
  YAML o `--json`), sin código para ese tipo.
- **El panel grafica cualquier tipo**: arma solo una gráfica por grupo de
  campos (`ax ay az`, `q[0…11]`, structs anidados) con el valor actual, y
  muestra topic y esquema.
- **Vectores de prueba compartidos** (`tests/vectors/v1.txt`): C++ y Python
  comparan byte por byte en el CI (reto #3 del RFC 0001).
- Prueba de sintaxis del JavaScript del panel (con Node).
- Tutorial: capítulo 8 (tus propios mensajes y `agmen echo`); capítulos 1–5
  actualizados al formato v1.

### Cambiado
- El mux usa el formato v1: a la salida pone su propio `source_id`, y los
  anuncios de esquema no le quitan el mando a nadie.
- El nodo `motor` del tutorial muestra cada error distinto una sola vez.
- CMake se reconfigura solo cuando cambia `VERSION`.
- `docs/PROTOCOLO.md` describe ahora el formato v1.

## [0.3.0] — 2026-11-02

**¿Quién manda?** Prioridad de comandos entre el panel y el modo autónomo, y
la prueba de que Agmen trabaja en paralelo y no en serie.

### Agregado
- `[[mux]]` en `robot.toml` y el comando `agmen mux`: varias fuentes le
  mandan al mismo actuador y manda la de prioridad más alta que esté
  hablando (`hold_ms`); al callarse, el mando pasa sola a la siguiente.
  Nunca inventa comandos, rechaza tipos distintos y renumera `seq`. Avisa en
  los logs cada cambio de mando y al final cuánto pasó de cada fuente.
- El robot del tutorial usa un mux: manejar desde el panel le gana al
  cerebro, sin apagarlo. `cerebro.py --destino`.
- `scripts/paralelo_check.py`: mide en tu computadora (y en el CI) que los
  nodos usan varios núcleos, que un nodo atorado o que imprime sin parar no
  frena a los demás, que Ctrl+C los detiene a todos a la vez y que el panel
  atiende a varios navegadores al mismo tiempo.
- CI en **Linux ARM64** (`ubuntu-24.04-arm`, el procesador de una Raspberry Pi
  con sistema de 64 bits).
- Tutorial: capítulos 6 (¿Quién manda?) y 7 (¿En paralelo o en serie?).

### Corregido
- Un punto en serie escondido: si un nodo imprimía sin parar en una terminal
  lenta (como la de Windows), los demás nodos se bloqueaban en su `print()` y
  su ciclo se atrasaba (un nodo de 50 Hz llegó a 1.5 s de atraso). Ahora
  la salida de cada nodo se lee sin esperar nunca a la terminal; un hilo
  aparte escribe desde una cola acotada y avisa si se saltó líneas, y cada
  nodo muestra hasta 200 líneas/s (el resto se resume).

### Cambiado
- Roadmap: el formato v1 (nivel 1, RFC 0001) pasa a la versión 0.4.0; los
  topics a 0.5.0 y el panel en C++ a 0.6.0.
- GitHub Actions actualizadas a versiones con Node 24 (adiós a los avisos de
  Node 20).

## [0.2.0] — 2026-10-30

**Todo el robot con un solo comando** y un panel web para controlarlo
(RFC 0003). Se acabaron las diez terminales.

### Agregado
- Comando `agmen` (se instala con `pip install agmen`).
- `agmen run robot.toml`: lanza todos los nodos descritos en un archivo, junta
  sus logs en una sola terminal con color por nodo, los reinicia si fallan
  (`restart = "on-failure"`), compila antes si se pide (`before`) y con Ctrl+C
  los detiene a todos de forma ordenada. Errores de configuración con
  sugerencias ("¿Quisiste decir 'name'?").
- Panel web de control: estado de cada nodo con botones iniciar / detener /
  reiniciar, logs de todos los nodos con filtros, gráficas de sensores y
  **manejo del robot** con teclado (W A S D) o botones táctiles. Al soltar, el
  robot frena.
- `agmen web`: el panel solo, sin administrar nodos.
- `examples/tutorial/robot.toml` y capítulo 5 del tutorial.
- Pruebas `scripts/run_smoke.py` en el CI.

### Cambiado
- `tools/agmen-web/bridge.py` se reemplaza por `agmen web`, incluido en el paquete.

### Corregido (encontrado por el primer CI en GitHub)
- Windows: los nodos de Python ya no truenan al imprimir acentos o "→" cuando
  su salida va a un archivo u otro programa (`agmen.ensure_utf8_output()`).
- macOS: el panel ya no tarda en arrancar por una consulta DNS lenta.

### Seguridad
- El panel rechaza peticiones de otros sitios (protección CSRF), recorta los
  comandos de manejo a la velocidad máxima y limita su frecuencia.

## [0.1.0] — 2026-10-30

Primera versión pública: **Nivel 0**.

### Agregado
- Núcleo en C++20 (`#include <agmen/agmen.hpp>`, `namespace agmen::`) con CMake.
- Formato de cable v0: header de 32 bytes, `type_id` por hash del esquema y
  errores claros (`agmen::wire::WireError`).
- Socket UDP multiplataforma (Windows/Linux/macOS) y sleep de alta resolución
  en Windows.
- Ejemplos `publisher` y `subscriber` con medición de latencia y pérdidas.
- Paquete de Python `agmen` (implementación de referencia del formato).
- Prototipo de interfaz web `tools/agmen-web` (el navegador dibuja; el robot
  solo publica).
- Tutorial: capítulos 0 a 4.
- RFC 0001 (formato de mensajes v1, **aceptado**) y RFC 0002 (interfaz web,
  propuesta).
- CI en Windows (MSVC), Linux (GCC + sanitizers) y macOS (Clang).

[Sin publicar]: https://github.com/flkochu/Agmen/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/flkochu/Agmen/releases/tag/v0.2.0
[0.1.0]: https://github.com/flkochu/Agmen/releases/tag/v0.1.0
