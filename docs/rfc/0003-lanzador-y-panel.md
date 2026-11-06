# RFC 0003 — Todo el robot con un solo comando: `agmen run` y el panel de control

| | |
|---|---|
| **Estado** | Implementado en Agmen 0.2.0 (`python/agmen/run.py`, `python/agmen/panel/`); paralelismo y `[[mux]]` en 0.3.0 (§4.1, §4.2); topics en 0.5.0; panel por WebSocket, modo red con clave y Foxglove en 0.6.0 (RFC 0002) |
| **Fecha** | 2026-10-30 |
| **Relacionado** | RFC 0002 (interfaz web) |

## 1. Problema

Trabajar con un robot en ROS 2 suele verse así:

| Terminal | Para qué |
|---|---|
| 1 | `ros2 launch` del robot |
| 2 | el driver del sensor que no venía en el launch |
| 3 | `ros2 topic echo /imu` para ver si llegan datos |
| 4 | `ros2 topic echo /cmd_vel` para ver qué órdenes se mandan |
| 5 | `teleop_twist_keyboard` para manejar (y la terminal tiene que estar enfocada) |
| 6 | `rqt` o RViz (pesados, normalmente en la computadora del robot) |

Los logs de todos los nodos salen mezclados, no hay forma fácil de reiniciar
un solo nodo y, si algo se cae, te enteras tarde.

## 2. Propuesta

```
agmen run robot.toml
```

Un comando que:

1. Lee `robot.toml` y lo **valida con errores claros** antes de arrancar nada.
2. Opcionalmente **compila** antes (`before`).
3. **Lanza todos los nodos**, en el lenguaje que sean.
4. Junta su salida en **una sola terminal**, con el nombre de cada nodo en color.
5. Los **supervisa**: reinicio automático con espera creciente (1, 2, 4… hasta 10 s).
6. Abre el **panel web**, donde se ve y se controla todo.
7. Con **Ctrl+C detiene todo** de forma ordenada y sin dejar procesos huérfanos.

## 3. Formato de `robot.toml`

```toml
[robot]
name = "mi robot"
before = ["cmake --build build"]          # opcional: comandos previos (si fallan, no arranca nada)

[[node]]                                  # un bloque por nodo
name = "motor"                            # único
cmd = "./build/bin/motor --puerto 7500"   # texto o lista: ["./build/bin/motor", "--puerto", "7500"]
restart = "on-failure"                    # never (default) | on-failure | always
autostart = true                          # false = empieza apagado; se prende desde el panel
cwd = "."                                 # opcional; default: la carpeta de robot.toml
env = { NIVEL_LOG = "2" }                 # opcional

[[mux]]                                   # opcional (0.3.0): ¿quién manda? Ver §4.2
name = "ruedas"                           # aparece como un nodo más
output = "/cmd"                           # a dónde reenviar: un topic (0.5.0) o "ip:puerto"
hold_ms = 300                             # sin mensajes en 300 ms → la entrada suelta el mando

[[mux.input]]                             # una por fuente; priority más alta = manda más
name = "teleop"
topic = "/cmd/teleop"                     # un topic (0.5.0)… o listen = "127.0.0.1:7510"
priority = 10

[[mux.input]]
name = "cerebro"
topic = "/cmd/cerebro"
priority = 1

[[topic]]                                 # opcional (0.5.0): los topics que esperas (RFC 0004 §4.1)
name = "/imu"
type = "ImuSample"                        # opcional: avisa si llega con otro tipo
required = true                           # opcional: avisa si nadie lo publica
max_gap_ms = 100                          # opcional (0.13.0): avisa si pasa más que esto sin llegar, y cuando vuelve

[hub]
enabled = true                            # false = no arrancar el agmen hub (sin topics)

[web]
enabled = true
http = "127.0.0.1:8080"                   # "0.0.0.0:8080" (o agmen run --red) = visible en tu red, con clave
listen = "127.0.0.1:7400"                 # además de todos los topics: para nodos sin hub
open_browser = true
token = "una-clave-larga"                 # opcional (0.6.0): clave fija del modo red; si no, una nueva cada vez
allow_origins = []                        # opcional (0.6.0): otros visores que pueden abrir el WebSocket (solo lectura)

[web.teleop]                              # opcional: manejar el robot desde el panel
target = "/cmd/teleop"                    # topic o "ip:puerto" de quien recibe MotorCommand
max_speed = 1.0                           # límite que el panel nunca excede

[[web.command]]                           # opcional (0.13.0, RFC 0012): una tarjeta de mandos de cualquier tipo compact
name = "Brazo"                            # el título de la tarjeta (default: el target)
target = "/cmd/brazo"                     # topic o "ip:puerto" (con mux, su entrada)
type = "Brazo"                            # MotorCommand, ImuSample o un mensaje de `messages`
messages = "brazo.agmen"                  # el .agmen de los nodos, relativo a robot.toml

[web.command.default]                     # opcional: los campos que no dice un botón
pinza = false

[[web.command.button]]                    # mientras lo mantienes presionado, manda este mensaje (~20 Hz)
label = "guardar"
value = { codo = 90.0, hombro = 10.0 }    # con default, el mensaje completo; si falta un campo, no arranca

[web.command.fields]                      # opcional: armarlo a mano
codo = { min = 0, max = 180 }             # un deslizador por número (min y max obligatorios; step y label opcionales)
pinza = {}                                # una casilla por bool
```

Desde 0.5.0, `agmen run` agrega un nodo **`hub`** (el directorio de topics,
RFC 0004) antes que los demás, o reutiliza uno que ya esté vivo en la
máquina. Le pasa los `[[topic]]` para que avise de errores. Desde 0.13.0,
cada `[[mux]]` publica quién manda en `/mux/<nombre>` (RFC 0012).

**Reglas:**
- Las rutas son relativas a `robot.toml` en todos los sistemas operativos.
  En Windows se agrega `.exe` solo si hace falta.
- `python` / `python3` / `py` al inicio de `cmd` se reemplaza por el Python
  que corre `agmen`. El mismo archivo funciona en Windows y en Linux.
- Cualquier clave desconocida es un error con sugerencia ("¿Quisiste decir
  'name'?"). Una clave mal escrita no se ignora en silencio.
- Se eligió TOML porque es legible, tiene comentarios y no depende de la
  indentación como YAML. Python lo lee sin dependencias desde la 3.11.

## 4. Supervisión de nodos

| Estado | Significado |
|---|---|
| `corriendo` | El proceso está vivo |
| `detenido` | Lo detuvo el usuario (o nunca arrancó, si `autostart = false`) |
| `terminó` | Salió con código 0 por su cuenta |
| `falló` | Salió con código ≠ 0, o no se pudo ejecutar (el mensaje dice por qué) |
| `reiniciando` | Esperando para volver a arrancar según `restart` |

- **Apagado ordenado:** primero se le pide al nodo como un Ctrl+C (SIGINT, o
  CTRL_BREAK en Windows). Si a los 3 s sigue vivo se termina, y 2 s después
  se mata.
- Cada nodo corre en su **propio grupo de procesos**. El Ctrl+C de la
  terminal lo recibe `agmen` y los detiene en orden, en vez de que cada uno
  muera por su cuenta.
- Variables que reciben los nodos: `PYTHONUNBUFFERED=1` (para que los logs
  de Python salgan al momento), `PYTHONIOENCODING=utf-8` y `AGMEN_NODE=<nombre>`.

### 4.1 Paralelismo (0.3.0)

Cada nodo es un **proceso del sistema operativo**, así que los nodos corren
en paralelo en núcleos distintos y uno atorado no detiene a los demás. Lo
único que comparten es la **terminal**, y ahí estaba el único punto en serie
de 0.2.0: si la terminal era lenta (la consola de Windows lo es) y un nodo
imprimía sin parar, los demás se bloqueaban en su propio `print()` y su
ciclo de control se atrasaba (medido: un nodo de 50 Hz con 1.5 s de atraso).
Desde 0.3.0:

- Un hilo por nodo lee su salida y **nunca espera** a la terminal ni al panel.
- Un hilo aparte escribe en la terminal desde una cola acotada (500 líneas).
  Si la terminal no da abasto, descarta líneas de la terminal (no del panel)
  y lo avisa: `… N líneas no cupieron en la terminal`.
- Cada nodo muestra hasta **200 líneas/s**. El excedente se resume en
  `… N líneas omitidas`. Los avisos de Agmen y los errores nunca se descartan.
- El apagado detiene a todos los nodos a la vez (4 nodos que tardan 1 s en
  cerrar → 1 s, no 4 s).

`scripts/paralelo_check.py` mide todo esto en el CI y en tu computadora.
Dentro de cada nodo el código sí corre en orden (un ciclo): si una parte de
un nodo no debe esperar a otra, sepáralas en dos nodos (o, desde el nivel 5,
usa varios temporizadores en un mismo nodo).

### 4.2 Prioridad de comandos: `[[mux]]` (0.3.0)

Si dos fuentes (el panel y un cerebro autónomo) mandan al mismo receptor, el
receptor obedece a las dos alternadamente y el robot se mueve a tirones. Un
`[[mux]]` es un nodo intermedio (`agmen mux`, se lanza solo) con una entrada
UDP por fuente:

- Manda la entrada de **prioridad más alta que haya hablado en los últimos
  `hold_ms`**; los mensajes de las demás se descartan. Cuando se calla, el
  mando pasa sola a la siguiente.
- **Nunca inventa comandos:** si nadie habla, no reenvía nada y el freno de
  seguridad del receptor actúa igual que sin mux.
- Todas las entradas deben mandar el mismo tipo (`type_id`); lo demás se
  descarta con aviso. La `seq` se renumera para que el receptor vea una sola
  fuente continua.
- Avisa en los logs cada cambio: `manda 'teleop' (prioridad 10)`.
- Las prioridades deben ser distintas y los puertos no se pueden repetir; si
  no, `robot.toml` no carga.

Equivale a `twist_mux` de ROS, pero sirve para **cualquier** tipo de mensaje.

## 5. El panel

| Sección | Qué hace |
|---|---|
| **Nodos** | Estado, tiempo encendido, pid y reinicios de cada nodo, con botones ▶ ■ ↻. Clic en un nodo = filtrar sus logs |
| **Manejar** | Botones ↑ ↓ ← → (táctiles) y teclado **W A S D** o flechas, con velocidad ajustable y barras de cada rueda. **Solo manda mientras presionas**: al soltar, cambiar de pestaña o perder la conexión, manda alto, y el freno de seguridad del motor hace el resto |
| **Datos** | Los topics con su tipo y frecuencia: clic para graficar (varios a la vez, 0.6.0). Hz, latencia, pérdidas, errores, esquema, y una gráfica por grupo de campos (armadas solas a partir del esquema). Si dos emisores mandan al mismo topic, lo avisa en vez de mezclar datos |
| **Mensaje** (0.6.0) | Inspector: el último mensaje del topic elegido, campo por campo, con su `seq`, emisor, hora y tamaño. Se puede congelar |
| **CPU del panel** (0.6.0) | Cuánto CPU usa el puente en el robot |
| **Logs** | Todos los nodos, con color por nodo, filtros, "seguir" y "limpiar". Errores en rojo y alertas en amarillo |

### API

El panel usa un **WebSocket** (`/ws`, protocolo `agmen.ws.v1`, RFC 0002 §5):
datos en vivo (los bytes de Agmen tal cual), estado, logs, manejo y control
de nodos. Para scripts y pruebas sigue habiendo HTTP:

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/ws` | WebSocket del panel (`agmen.ws.v1`) y de Foxglove (`foxglove.websocket.v1`) |
| GET | `/api/state` | Robot, versión, nodos, estadísticas por topic, teleoperación, `cpu_s` del puente; desde 0.13.0, `commands` (los mandos), `muxes` (quién manda en cada mux) y `gaps` (los topics de `max_gap_ms`) |
| GET | `/agmen.js` | El decodificador del formato en JavaScript |
| POST | `/api/node/<nombre>/start\|stop\|restart` | Controlar un nodo |
| POST | `/api/teleop` `{"left": x, "right": y}` | Mandar un `MotorCommand` (a `[web.teleop] target`; con mux, a su entrada `teleop`) |
| POST | `/api/command` `{"command": "Brazo", "button": "guardar"}` o `{"command": 0, "fields": {"codo": 45}}` | Un mando de `[[web.command]]` (0.13.0, RFC 0012): el panel revisa los valores contra el esquema y sus rangos, y arma el mensaje |

`/api/stream` (Server-Sent Events, 0.2–0.5) se quitó en 0.6.0: lo reemplaza el WebSocket.

## 6. Seguridad

| Riesgo | Mitigación |
|---|---|
| Una página maliciosa abierta en tu navegador manda comandos a `127.0.0.1:8080` (CSRF) | Todo POST exige el encabezado `X-Agmen: 1`, que un sitio ajeno no puede mandar sin permiso, y si viene `Origin`, debe ser el mismo panel |
| Alguien en tu red maneja el robot | Por defecto el panel solo escucha en `127.0.0.1`. En la red (`--red`) exige una clave que la terminal imprime en el link (0.6.0, RFC 0002 §6) |
| Un bug del navegador manda velocidades absurdas o satura al robot | El panel recorta a `max_speed` y limita a ~60 comandos/s |
| Se pierde la conexión mientras manejas | El puente manda ALTO si no llegan comandos en 300 ms (0.6.0); además el motor frena por su timeout de seguridad |

**Antes del modo nube** (RFC 0002): usuarios, contraseñas y TLS.

## 7. ¿Por qué el comando `agmen` está en Python si el núcleo es C++?

Orquestar procesos, servir un panel y leer TOML no es el camino crítico: no
mueve mensajes a 1 kHz. En Python es mucho más corto y fácil de contribuir, y
se instala con `pip install agmen` en los tres sistemas. ROS 2 hace lo mismo:
su comando `ros2` también está escrito en Python. Los nodos y el núcleo
siguen en C++ (o en el lenguaje que quieras).

## 8. Limitaciones del prototipo y siguientes pasos

| Hoy (0.6.0) | Siguiente |
|---|---|
| El mux está en Python (sobra para comandos de 20–100 Hz como los del tutorial; agrega un salto local de UDP) | Pasarlo al núcleo C++ si se usa en lazos de control más rápidos |
| Grafica cualquier tipo de varios topics a la vez, con inspector (0.6.0) | Cámara (nivel 7), 3D y grabaciones (nivel 9); topics de otras máquinas (nivel 11) |
| La teleoperación manda `MotorCommand` a un topic. Desde 0.13.0, `[[web.command]]` manda **cualquier tipo `compact`** desde el panel: botones con un mensaje fijo y deslizadores y casillas con sus rangos, con hombre muerto ([RFC 0012](0012-mandos-del-panel-y-quien-manda.md)) | Publicar desde Foxglove; tipos `arrow` (textos) desde el panel; atajos de teclado para los botones |
| El mux solo dice quién manda en su log. Desde 0.13.0 lo publica en `/mux/<nombre>` (`agmen.msgs.MuxEstado`) y el panel lo muestra, con un aviso cuando la entrada que mandaba se calla (RFC 0012) | — |
| `[[topic]] required` avisa si nadie publica un topic. Desde 0.13.0, `max_gap_ms` avisa si deja de llegar y cuando vuelve (RFC 0012) | — |
| Un solo robot y una sola máquina | Nodos en varias máquinas (`host = "raspberry"`); varios robots en un panel (modo nube, RFC 0002) |
| Clave para la red local (sin cifrar) | Usuarios, permisos y TLS para el modo nube (nivel 11) |
| CPU y memoria por nodo: no se muestran | Agregarlos sin dependencias externas |
