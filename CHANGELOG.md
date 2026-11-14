# Changelog

Todos los cambios importantes de Agmen se anotan aquí. Formato basado en
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según
[SemVer](https://semver.org/lang/es/). Política completa en
[`docs/VERSIONES.md`](docs/VERSIONES.md).

## [Sin publicar]

### Planeado (nivel 10, versión 0.14.0)
- Nodos en varias computadoras. Diseño aceptado el 2026-11-13 en el
  [RFC 0011](docs/rfc/0011-sim-real-y-varias-computadoras.md) (D1–D10 = a:
  perfiles en `robot.toml`, un mundo de MuJoCo de ejemplo y `agmen agent` en
  cada computadora). Hechos: los perfiles (bloque 1, abajo) y el mundo de
  MuJoCo (bloque 2, abajo). Falta: `[[machine]]`, `agmen agent`, topics entre
  computadoras y relojes (bloques 3 a 5 del plan del RFC).

### Agregado
- **Perfiles en `robot.toml`** (RFC 0011 §3.2): `profile = "real"`, `"sim"`,
  `"replay"` o un nombre propio (`"mesa"`) en `[[node]]`, `[[mux]]` y
  `[[topic]]`. Sin `profile`, corren en todos. Un perfil puede ser una lista
  (`profile = ["real", "mesa"]`). Dos nodos pueden llamarse igual si nunca
  corren juntos (una `imu` real y una `imu` falsa).
- **`[profile.X]`** con `clock`, `web` y `record`: cada una reemplaza la de
  arriba, solo en ese perfil. `local` se acepta, pero todavía no hace nada
  (llega con varias computadoras).
- **`[robot] profile`**: el perfil por defecto de `agmen run`. Por omisión,
  `real`.
- **Banderas de `agmen run`**: `--sim` (lo mismo que `--profile sim`, con el
  reloj de simulación sin escribir `[clock]`) y `--profile PERFIL`.
- **Letrero del perfil**: la terminal dice `perfil sim · reloj de simulación
  (×1) · no corren: …` cuando el perfil no es `real` o hay nodos que no
  corren. El panel muestra en grande SIMULACIÓN, REPRODUCCIÓN o PERFIL X;
  `/api/state` trae `profile` y `playing`.
- **Avisos y errores con sugerencia**: un perfil que no existe (con la lista
  de los conocidos), un error de dedo (`profile = "rael"` → `¿Quisiste decir
  'real'?`), `--skip` de un nodo de otro perfil (aviso), `--sim` con `--play`
  (error) y el perfil `replay` sin `--play` (error).
- **`perfil` en las grabaciones**: `agmen run --record` lo guarda en
  `agmen.grabacion`, y `agmen info` lo muestra. `agmen record` suelto no lo
  escribe. Las grabaciones sin el campo se leen igual.
- **El mundo de MuJoCo** (RFC 0011 §3.3, `examples/sim/`): el robot del
  tutorial con física, como un nodo más con `profile = "sim"`. Publica `/imu`
  (100 Hz) y `/camara` (`Imagen` de 320 × 240, 15 fps), y escucha `/cmd`, igual
  que el hardware. `agmen run examples/tutorial/robot.toml --sim` lo corre; el
  cerebro, el mux y el panel no cambian. El mundo sigue al reloj de Agmen, así
  que la pausa, el paso y la velocidad del panel funcionan.
  - Cuatro archivos: `mundo.py` (el nodo), `fisica.py` (la clase `Mundo`, sin
    Agmen), `camara.py` (la cámara) y `ver.py` (la ventana de `--ver`).
  - `--ver` abre la ventana de MuJoCo. Sin pantalla, avisa y sigue.
  - Sin OpenGL, el mundo sigue sin cámara y lo dice en una línea.
  - La clase `Mundo` (`fisica.py`) se puede usar sola, sin Agmen (pensada para
    entrenar IA).
- **`agmen[sim]`**: extra de `pip` que trae MuJoCo (`mujoco>=3.2.7`).
- **Capítulo 17 del tutorial**: el mismo robot en simulación y en el mundo
  real.

### Cambiado
- **El robot del tutorial usa perfiles.** `imu` y `motor` son
  `profile = "real"`, y `mundo` es `profile = "sim"`. Por eso `agmen run
  examples/tutorial/robot.toml` imprime `no corren: mundo (perfil sim)`, y
  `--sim` no arranca el motor.
- **`--play` ya no arranca los nodos `profile = "real"`.** Usa el perfil
  `replay`: un motor no debe moverse con los datos de una grabación. Para
  correrlos, `agmen run robot.toml --play ARCHIVO.mcap --profile real`
  (con el reloj de la grabación, y `agmen run` avisa). Los `robot.toml` sin
  perfiles no cambian: `--play` arranca los mismos nodos que en 0.13.0, y
  `--skip` funciona igual.

### Corregido
- **`cmd` con la ruta de un ejecutable** (`cmd = "../venv/bin/python nodo.py"`)
  se corre tal cual, sin seguir los enlaces simbólicos. Antes acababa en el
  Python del sistema y no en el del entorno virtual. `python` a secas sigue
  siendo el Python que corre `agmen`.
- **El botón "paso" del panel** ya no cuenta como ciclos saltados ni imprime
  `⚠ every(…): N ciclos saltados…`. Los atrasos de verdad se siguen contando y
  avisando. Sin cambio en los bytes de la red.
- **El vigilante de `max_gap_ms`** ya no da la alarma falsa `'/imu' no llega`
  al dar pasos: tras cada paso, el topic tiene 0.3 s de reloj real para
  ponerse al día antes de avisar. Un topic que de verdad dejó de llegar se
  avisa como mucho 0.3 s más tarde.
- **Un programa de Python que publica ya no aborta a veces al salir** con
  `terminate called without an active exception` (pasaba con Python 3.12 y el
  núcleo de C++, desde 0.9.0): el hilo de latidos al hub ahora se detiene
  antes de que el intérprete termine. Si tu programa publica o recibe desde un
  hilo propio en segundo plano, deténlo y espéralo (`join`) antes de salir:
  ese caso todavía puede abortar con Python 3.12.

## [0.13.0] — 2026-11-06

**El panel manda cualquier tipo, y el mux dice quién manda.** Implementa el
[RFC 0012](docs/rfc/0012-mandos-del-panel-y-quien-manda.md) con las opciones
recomendadas (D1–D6 = a), a pedido de Arca9 (los puntos 1 y 2 de su
`pc/agmen/PENDIENTES_AGMEN.md`). Los bytes de la red no cambian: un nodo de
0.13.0 habla con uno de 0.12.x, y el mux reenvía lo mismo que antes.

### Agregado
- **Mandos del panel para cualquier tipo `compact`** (`[[web.command]]` en
  `robot.toml`): una tarjeta con botones que mandan un mensaje fijo y, si
  quieres, deslizadores y casillas de los campos que digas (`[web.command.fields]`,
  con `min` y `max`). El tipo sale del mismo `.agmen` que usan los nodos
  (`messages = "arca9.agmen"`), o es uno de Agmen (`MotorCommand`, `ImuSample`).
  - **Hombre muerto**: el navegador manda solo mientras mantienes presionado
    (20 por segundo); al soltar, cambiar de pestaña o perder la conexión, se
    calla. No se inventa un "alto": el receptor aplica su watchdog.
  - **El panel arma el mensaje**: el navegador pide "el botón 1" o "estos
    campos", y el panel revisa cada valor contra el esquema y su rango (NaN,
    infinitos, decimales en un entero, campos sin control: no). Nunca reenvía
    bytes del navegador.
  - Un botón siempre manda un mensaje completo: lo que no dice sale de
    `[web.command.default]`, y si falta un campo, `agmen run` no arranca.
  - La misma seguridad que el teleop (clave en `--red`, `X-Agmen`, `Origin`,
    máx. ~60 Hz); también por `POST /api/command`, para scripts.
  - La tarjeta dice a qué entrada del mux va y, mientras mandas, si te gana
    otra entrada. El robot del tutorial trae una (**Movimientos**).
- **El mux publica quién manda** en `/mux/<nombre>` (`agmen.msgs.MuxEstado`,
  también en C++): la entrada que manda, su topic y prioridad, desde cuándo,
  la anterior y si se calló (`callada`), el `source_id` que manda y cuántos
  cambios van. Cada vez que cambia y una vez por segundo. `agmen run` lo
  arranca así (`agmen mux --state /mux/<nombre>`); es un topic de Agmen, como
  `/clock`: no hace falta declararlo.
- **El panel muestra quién manda** (tarjeta *¿Quién manda?*) y avisa cuando la
  entrada que mandaba se calla: `⚠ ruedas: 'teleop' se calló → manda 'cerebro'`.
- **`[[topic]] max_gap_ms`**: `agmen run` avisa cuando un topic deja de llegar
  y cuando vuelve, en la terminal y en el panel (`required` solo ve si nadie
  lo publica). Lo mide con el reloj de Agmen: en pausa no avisa.
- `/api/state` trae `commands`, `muxes` y `gaps`.

### Cambiado
- `examples/tutorial/robot.toml`: la tarjeta **Movimientos** y
  `max_gap_ms = 100` en `/imu`. Tutorial: capítulos 5, 6 y 10.
- `scripts/check_version.py` también revisa la línea "Versión actual" del
  README (decía 0.12.0).

### Corregido
- **Al detener una grabación desde el panel (⏹), mientras se cerraba el
  archivo el panel decía "no graba" y 0 mensajes** (o los de la grabación
  anterior), desde 0.12.0. `RecordingControl.stop()` soltaba la grabación
  antes de cerrarla. Ahora sigue diciendo "grabando" hasta que el archivo
  queda cerrado y después da su resumen. Si `agmen run` se detiene (Ctrl+C)
  mientras el panel cierra, espera a que termine en vez de salir con el
  archivo a medias. Lo encontró `test_web` en el CI de Windows, donde cerrar
  tardó más de 0.2 s.

## [0.12.1] — 2026-11-06

Parche: dos errores que encontró Arca9 al usar Agmen (los puntos 4 y 7 de su
`pc/agmen/PENDIENTES_AGMEN.md`). Los bytes de la red no cambian: un nodo de
0.12.1 habla con uno de 0.12.0.

### Corregido
- **NaN e infinitos salen como `null` en el JSON** (`schema.jsonable`): en lo
  que el panel le manda a Foxglove (en vivo y con `agmen export --foxglove`) y
  en `agmen echo --json`. Antes salía `[NaN, 1.0]`, que no es JSON: un lector
  estricto (el `JSON.parse` del navegador, Foxglove) rechazaba el mensaje
  completo, no solo ese número. Arca9 pone NaN en un servo sin lectura
  creíble. Vale también para arreglos de NumPy y tensores.
- **Dos `agmen run` en la misma máquina**: si el `[web] listen` del panel
  (`127.0.0.1:7400`) ya lo usa otro robot o un `agmen web`, el panel lo dice
  con palabras y sigue con los topics. Antes su hilo tronaba con
  `OSError: [Errno 98] Address already in use` y el panel se quedaba sin datos.
- Y el segundo `agmen run` **dice de quién es el hub que reusa y avisa si los
  dos robots usan los mismos topics**, porque sus mensajes se mezclan (el
  `poses.py` de uno le mandaba al puente del otro):

  ```
  ⚠ ya hay otro robot en esta máquina, 'Arca9 (simulado)' (su agmen hub está en 127.0.0.1:7390),
    y los dos usan /arca9/comando, /arca9/estado: sus mensajes se van a mezclar …
  ```

  Para eso `agmen run` arranca el hub con su nombre (`agmen hub --robot`), y
  la línea `state` del hub lo dice (`robot=`). Un hub de 0.12.0 no lo dice: el
  aviso sale igual, sin el nombre.

## [0.12.0] — 2026-11-05

**Nivel 9: grabar y reproducir; tu robot como dataset.** Implementa el
[RFC 0010](docs/rfc/0010-grabar-y-reproducir.md) (D1–D9 = a): las grabaciones
son archivos MCAP con los mensajes de Agmen adentro, sin cambiar un byte. Los
bytes de la red no cambian: un nodo de 0.12.0 habla con uno de 0.11.x.

### Agregado
- **`agmen record`**: graba topics en un `.mcap` (todos, o los que digas) como
  un suscriptor más, por memoria compartida cuando puede. **Nunca frena a
  nadie**: una cola con tope (lo menor entre 256 MiB y ⅛ de la RAM; `--queue`
  la cambia); si el disco no alcanza, descarta, lo avisa y deja escritas las
  pérdidas por topic y por qué (disco, huecos de `seq`, topic sin anuncio).
  Chunks de ~4 MiB o 1 s: si el grabador se cae, se pierde como mucho el
  último segundo. `--split 1GiB|10min`, `--compress zstd|lz4`, `--duration`.
- **`agmen info`**: resume una grabación (duración, topics, tipos, mensajes,
  pérdidas, adjuntos); `--reparar` le reescribe el índice a una cortada.
- **`agmen play`**: reproduce cada mensaje a su tiempo (`--rate`, `--loop`,
  `--start`, `--end`) con los mismos bytes, salvo el campo `clock` = replay,
  y publica `/clock` con la hora de la grabación. Al terminar, el tiempo
  queda en pausa al final.
- **El reloj `replay`** (RFC 0006) en C++ y en Python: como `sim` (sigue
  `/clock`, interpola, nunca va hacia atrás) con el tiempo de una grabación.
  Un nodo con reloj real que recibe un mensaje de una grabación lo avisa.
- **`agmen run`**: `--record`, `[record]` en `robot.toml` (`topics`, `dir`,
  `split`, `compress`, `auto`) y el **botón ⏺ grabar** del panel;
  `--play ARCHIVO.mcap` (todos los nodos en `replay`), `--play-topic` y
  `--skip NODO` (la grabación sustituye al sensor). El panel muestra el reloj
  de la reproducción, con pausa, paso y velocidad.
- **El panel abre grabaciones**: `agmen web vuelta1.mcap`, o arrastrar el
  archivo (o *Grabación → abrir .mcap…*). El navegador lo lee **por partes**
  con el índice de MCAP (`mcap.js`, sin comprimir o lz4; zstd lo descomprime
  `agmen web`) y lo decodifica con `agmen.js`: gráficas, cámara, inspector y
  una barra de tiempo para reproducir, pausar, cambiar la velocidad y saltar.
- **`agmen.Recording`**: una grabación en Python. `messages()` da cada mensaje
  como un `agmen.Sample` (con `.topic` y `.log_time`) con su clase (la tuya, o
  una armada con el esquema grabado); `table(topic)` da una `pyarrow.Table`
  (`t_data`, `t_pub`, `log_time`, `source_id`, `seq` y los campos con su tipo
  de Arrow); `info`, `lost`, `attachments`, `duration`.
- **`agmen export`**: `--parquet CARPETA` (una tabla por topic, escrita por
  partes) y `--foxglove SALIDA.mcap` (los mensajes en JSON, las imágenes como
  `foxglove.RawImage`/`CompressedImage`, como el puente en vivo).
- `agmen._mcap`: lector y escritor de MCAP en Python puro, sin dependencias.
  La biblioteca oficial de MCAP los juzga en el CI (en los dos sentidos), y un
  archivo cortado en cualquier byte se lee hasta el último chunk completo.
- Extras de pip: `agmen[grabar]` (zstandard y lz4) y `agmen[datasets]`
  (pyarrow).
- `tests/vectors/grabacion.mcap` (y `generar_mcap.py`): la leen igual Python,
  JavaScript y la biblioteca oficial. `docs/PROTOCOLO.md` documenta el perfil
  `agmen` de MCAP.
- `bench/grabar.py`: MB/s, pérdidas y CPU del grabador con una cámara de
  640×480 a 4K. `scripts/grabar_smoke.py` en el CI: grabar con `agmen run
  --record` y el botón, `agmen info`, y reproducir con `agmen run --play
  --skip imu`.
- Tutorial, [capítulo 16](docs/tutorial/16-grabar-y-reproducir.md): grabar y
  reproducir, tu robot como dataset.

### Cambiado
- Las herramientas de Agmen se anuncian en el hub con su nombre (`agmen run`,
  `agmen play`, `agmen record`…). Con el núcleo de C++ aparecían como `-m`.
- `[clock] mode = "replay"` en `robot.toml` dice que se usa
  `agmen run --play` (antes decía "llega en el nivel 9").
- `agmen run` detiene los nodos **antes** de cerrar la grabación (se guarda
  hasta el último mensaje) y el resumen sale antes del "listo".

### Corregido
- Windows: crear un bloque de memoria compartida con un nombre que ya existía
  **abría el ajeno** (CreateFileMapping), y dos publicadores podían escribir en
  la misma memoria. Ahora es un error y se usa otro nombre, como `O_EXCL` en
  Linux y `ERROR_ALREADY_EXISTS` en C++. El contador de los nombres es del
  proceso, no de cada publicador.
- `agmen.arrow`: los tipos de extensión de C++ de pyarrow
  (`fixed_shape_tensor`) no se reconocían con `pa.ExtensionType`.
- Windows: un suscriptor de mensajes `arrow` con dirección fija y puerto 0
  (`"127.0.0.1:0"`) podía fallar con `WinError 10013` si el puerto que el
  sistema le dio en UDP caía en un rango TCP reservado (Hyper-V, WSL y Docker
  apartan rangos enteros). Reintentar con otro UDP no servía: Windows da los
  puertos de corrido y caían en el mismo rango. Ahora se pide primero el TCP
  (el sistema nunca da uno reservado) y el UDP se abre en ese número, en C++ y
  en Python. Pasó en el CI de Windows.
- `agmen run` y `agmen web`: el panel ponía `AGMEN_NODE=panel` en el entorno
  de **todo el proceso**, desde su hilo, y lo que se creaba después en ese
  proceso (el reproductor de `--play`, una grabación empezada con el botón ⏺)
  se anunciaba en el hub como `panel`. Ahora el nombre va solo en los
  receptores y en el teleop del panel (`RawSubscriber(..., node=)`,
  `_PurePublisher(..., node=)`).

## [0.11.1] — 2026-11-05

**Parche de 0.11.0.** Tres cosas: la auditoría de robustez del decodificador
con *fuzzing* (PR #2), la decisión D6 (a′) del RFC 0009, ya aceptado (la
memoria compartida aparta según la computadora y se encoge sola), y **macOS
sin soporte oficial**. Los bytes no cambian: un nodo de 0.11.1 habla con uno
de 0.11.0 (solo se rechazan esquemas de más de 32 niveles o tipos fijos de más
de 2 GiB, que no existían).

### Corregido

**El decodificador** (PR #2, `tests/fuzz/`). Todo lo de abajo se podía
provocar con **un solo paquete** desde la red: un anuncio de esquema o un
mensaje `arrow`.

- **Un anuncio con structs anidados tumbaba a cualquier `Subscriber` de C++**
  (desbordamiento de pila): `read_announcement` leía el esquema con
  `wire::schema::check`, recursivo y con 4 KB de pila por nivel, y ~1 500
  niveles (un anuncio de 8 KB, un datagrama) bastaban. Lo encontró
  `fuzz_layout`. Ahora el anuncio se lee con `agmen::schema::Layout`, como en
  Python, y `check()` ya no guarda los nombres de los campos en la pila (eso
  también quita su límite de 256 campos por struct, que hacía que C++ rechazara
  anuncios válidos de Python).
- **En Python, el mismo anuncio (~500 niveles, 2.5 KB) lanzaba `RecursionError`**
  en vez de `WireError`, y `agmen echo`, el panel y `Subscriber` se caían. Y
  `agmen::schema::Layout` (C++) también desbordaba la pila, con ~3 000 niveles.
- **Límites de esquema iguales en C++, Python y JavaScript** (`docs/PROTOCOLO.md`):
  como mucho 32 niveles de structs y arreglos anidados, y un tipo de tamaño fijo
  mide como mucho 2 GiB − 1. Sin ellos:
  - en C++ el tamaño de `u8[1000000][1000000][1000000][1000000]` **daba la vuelta**
    (10²⁴ mod 2⁶⁴): el tamaño y los offsets salían mal, y decodificar podía leer
    fuera del payload;
  - en Python, `Layout("A{u8[1048576][64] x}")` tardaba **18 s** (armaba un formato
    con una letra por elemento) y lo pagaba cualquiera que recibiera el anuncio;
    `atriz{f32[6][1024288][3] m}`, de 27 bytes, lo encontró atheris. Ahora el
    formato se arma la primera vez que llega un dato y los arreglos de números
    van con su cuenta (`"307200f"`).
- `check()` aceptaba `tensor<f32,18446744073709551617>` (el número daba la
  vuelta a 1).
- Python: el tamaño de un arreglo anidado costaba **2^niveles** (`Array.size`
  llamaba dos veces al de su elemento): `u8[1][1]…` de 24 niveles tardaba
  2.5 s y uno de 40 colgaba al receptor.
- Python: un tamaño de arreglo o de tensor de más de 4 300 cifras lanzaba
  `ValueError` (de `int()`), no `SchemaError`.
- **arrow (C++): la forma de un tensor daba la vuelta**: 65536⁴ = 2⁶⁴ se
  contaba como 0 y pasaba un tensor "de 2⁶⁴ elementos" sin datos, al leer y al
  escribir. Ahora se cuenta exacto.
- arrow (Python): un tensor sin datos con una forma enorme
  (`(469762048, 402653184, 0, 67108864)`) lanzaba `ValueError` de NumPy, que no
  puede tener esa forma. Lo encontró atheris. En C++, Python y `agmen.js` vale
  ahora la regla de NumPy, al leer y al escribir: las dimensiones (un 0 cuenta
  como 1) por el tamaño del elemento caben en 63 bits.
- arrow (C++): al leer una columna se reservaba memoria para sus valores
  **antes** de revisar que el mensaje trajera los datos: 131 072 `bool` sin
  sus bits (33 KB) pedían 15 MB, y un mensaje de 64 MiB, decenas de GB
  (`std::bad_alloc`, que no es `WireError`). Ahora se reserva después.
- arrow (C++): codificar un `f64` fuera de rango (`1e30`, NaN) en un `i64`,
  `time`, `duration` o `u64` era comportamiento indefinido; ahora es un
  `SchemaError`, como en los demás enteros (también si no es entero).
- arrow (Python y `agmen.js`): **offsets negativos se aceptaban en silencio**
  (`data[-2:2]` toma bytes del final) y llegaba un texto equivocado; filas o
  columnas con largo negativo terminaban en `IndexError`, `struct.error` o
  `RangeError`. Ahora se rechazan como en C++ (y un cuerpo de largo negativo,
  que ya se rechazaba de rebote, se revisa de frente). `agmen.js` también
  rechaza tensores con dimensiones negativas (`[-1, -2]` "medía" 2).
- **Un paquete mal hecho detenía al `Node`** (C++, y Python con los dos
  núcleos): cualquier `WireError` de una suscripción (un anuncio inválido,
  basura, un `arrow` que no se puede leer) salía de `spin()`. Ahora el paquete
  se descarta, se cuenta (`invalid` en `node.subscriptions()`) y se avisa:
  `⚠ '127.0.0.1:7500': descarté un paquete inválido: …. El nodo sigue.` (el
  primero en el acto, luego uno cada 5 s con la cuenta). Que el hub rechace el
  topic sí lo detiene, como antes: es un error de configuración.
- **arrow (Python sin NumPy): un tensor recibido no se podía volver a
  publicar** (desde 0.10.0): llegaba como un `memoryview` con forma, y el
  codificador solo acepta `agmen.Tensor` o `numpy.ndarray`. Ahora llega como
  `agmen.Tensor`, lo que promete el RFC 0008, con los datos como una vista sin
  copiar. Lo encontró atheris en el CI.

**La memoria compartida** (RFC 0009, D6 = a′). 512 MiB fijos eran la mitad de
una Raspberry Pi 3 (1 GB), y lo que se apartaba en un pico quedaba apartado
hasta que el nodo terminaba.
- **El tope del pool depende de la computadora**: el menor entre 512 MiB, ¼ de
  la RAM física y, en Linux, ½ de `/dev/shm` (en una Raspberry Pi 3, 256 MiB;
  en Docker con el `/dev/shm` de 64 MB, 32 MiB). `AGMEN_SHM_MB` manda, aunque
  pase del ¼, y en C++ ya acepta fracciones como en Python (`0.5`). El aviso
  dice de dónde sale el tope: `… (256.0 MiB, ¼ de la RAM; cámbialo con
  AGMEN_SHM_MB)`.
- **El pool se encoge**: un bloque que nadie tiene prestado y que no se usó en
  5 s se libera (se borra su nombre, se suelta su memoria y sale del
  registro); el de prueba también. Nunca uno prestado ni uno que nombra un
  descriptor en camino. En la ráfaga 4K de las pruebas: 80 MiB apartados → 0
  a los 5 s, sin nada en `/dev/shm`.
- **El suscriptor de C++ suelta los bloques que ya no ve**: cierra los que
  ningún descriptor nombró en 5 s (en `receive()` y en el latido del `Node`).
  Antes los guardaba abiertos mientras durara la conexión, y en Windows la
  memoria de un bloque no se libera mientras alguien lo tenga abierto.

**Otros.**
- **Pickle de un tipo arrow escrito a mano** (`@dataclass` + `wire.Message`):
  una vez recibido, fallaba con `cannot pickle 'memoryview' object`; solo los
  tipos generados (`agmen gen`, `agmen.msgs`) sabían. Ahora `wire.Message` se
  lo da a todos los tipos arrow, también a sus structs anidados (dicts).
- Panel: el aviso "→ ALTO" del hombre muerto se escribe antes de mandar el
  alto (en una máquina lenta, quien ya había recibido el alto todavía no veía
  por qué; falló una vez `test_web` en Windows).

### Cambiado
- **macOS sin soporte oficial** (`docs/VISION.md`, §4): se prueba y se publica
  para **Windows y Linux, x86-64 y ARM64**. El CI ya no corre en
  `macos-latest` y el release ya no compila wheels para macOS (Intel ni Apple
  Silicon). **En una Mac, `pip install agmen` instala la wheel de Python puro**
  (`py3-none-any`): todo funciona igual, más lento. No se borró código:
  `shm.cpp` y `net.cpp` siguen siendo POSIX y `platform.cpp` conserva su
  `#ifdef __APPLE__`.
- `pyproject.toml`: clasificadores `Operating System :: Microsoft :: Windows`
  y `POSIX :: Linux` en vez de `OS Independent`.
- El publicador escribe en el bloque libre que se usó más recientemente (antes,
  en orden circular, que mantenía vivos a todos y el pool nunca se encogía).

### Agregado
- **Fuzzers** en `tests/fuzz/` (README ahí): `fuzz_wire`, `fuzz_layout` y
  `fuzz_arrow` con libFuzzer, ASan y UBSan (`-DAGMEN_FUZZ=ON`, Clang), y
  `fuzz_python.py` con atheris para los mismos tres blancos en Python. Parten
  de los vectores de `tests/vectors/` (`semillas.py`) y, además de buscar
  caídas, revisan que `check()` y `Layout` acepten lo mismo y que lo
  decodificado cumpla su esquema y se vuelva a codificar igual.
- **CI**: job `fuzz`, un minuto por fuzzer de C++ y 30 s por blanco de Python
  en cada push; si encuentra algo, la entrada queda en los artefactos.
- Constantes `wire::schema::kMaxDepth`, `kMaxFixedSize`, `kMaxArray` y
  `kMaxTensorDims`; en Python, `schema.MAX_DEPTH` y `schema.MAX_FIXED_SIZE`.
- `SubscriptionStats::invalid` (C++ y Python): los paquetes mal hechos que el
  `Node` descartó. En Python, `wire.HubRejected` (hija de `WireError`) cuando
  el hub rechaza publicar o escuchar en un topic.
- Memoria compartida: `pub.shm_bytes` (C++ `shm_bytes()`, los dos núcleos de
  Python), lo que un publicador tiene apartado ahora;
  `Subscriber::shm_mapped()` y `forget_idle_blocks()` en C++;
  `shm::limit_bytes()`, `limit_reason()`, `physical_ram()` y `devshm_size()`
  (en Python, `agmen._shm.limit()`).

### Pruebas
- **Pickle con memoria compartida** (`python/test_shm.py`, con los dos
  núcleos): una `Imagen` que llega por memoria compartida (`FLAG_SHM`) se hace
  pickle igual a lo publicado, con todos los protocolos; viaja por un `Pool` de
  `multiprocessing` con `spawn`; y el bloque vuelve al publicador al soltar el
  mensaje original aunque la copia siga viva (y el publicador lo reutiliza sin
  que la copia cambie). También un tipo escrito a mano con un struct anidado.
- **D6 (a′)**, en C++ (`tests/test_shm.cpp`) y con los dos núcleos de Python:
  el tope con 1 GB de RAM fingido (un gancho interno de las pruebas, no una
  variable de entorno), que `AGMEN_SHM_MB` manda, que el publicador lo
  respeta; el pool que se encoge, sin esperar (`trim(ahora + 6 s)`): nunca el
  prestado, y el registro dice lo que existe; y la ráfaga 4K con 5 s de
  silencio de verdad, que revisa también `/dev/shm` y `/proc/self/maps`.
- `tests/fuzz/regresiones/`: cada entrada que rompió un decodificador (las
  hechas a mano salen de `generar.py`). Las corren `ctest`
  (`fuzz_regresiones_*`, en los tres sistemas y sin Clang, con `replay.cpp`),
  `python/test_fuzz.py` y `python/test_panel_js.py` (por `agmen.js`).
- Una prueba con nombre por cada arreglo: `test_wire` (anuncios anidados y de
  300 campos, `check()` y `Layout` con los mismos límites, `static_assert` de
  los tamaños), `test_layout`, `test_arrow` (tensores, `f64` en enteros de 64
  bits, la memoria que pide una columna sin datos) y `python/test_fuzz.py`.
  Todas fallan (o se cuelgan) con el código anterior, menos la del cuerpo
  negativo.
- **Un `Node` en un hilo con la pila normal de Windows (1 MB)** recibe anuncios
  de 300 y 2 500 niveles, basura y un `arrow` ilegible, y luego todos los
  mensajes buenos: `test_node` (C++) y `TestNodo` en `python/test_fuzz.py`
  (con los dos núcleos). Con 0.11.0, el de C++ se caía por la pila y el de
  Python puro se detenía.
- `test_tensor_sin_numpy_se_puede_volver_a_publicar`: decodificar sin NumPy,
  volver a codificar (los mismos bytes) y pickle, también con forma vacía.

## [0.11.0] — 2026-11-05

**Nivel 8: memoria compartida** (RFC 0009, propuesto con las opciones
recomendadas). En la misma computadora, las imágenes y los tensores ya no se
copian entre procesos; tu código no cambia ni una línea.

### Agregado
- **Memoria compartida para los mensajes `arrow` de 4 KiB o más**: el
  publicador escribe el payload **una vez** en un bloque de memoria compartida
  y por la conexión TCP (RFC 0008) manda solo un descriptor de 72 bytes, con la
  bandera `SHM` del sobre (reservada desde el RFC 0001). El suscriptor recibe
  una vista del bloque, sin copiar, y se lo devuelve al publicador cuando ya
  nadie la usa. En Python, `as_array()` y `agmen.load(..., numpy=True)` dan un
  `numpy.ndarray` (de solo lectura) que apunta a la memoria de la cámara.
  Linux y macOS con `shm_open`, Windows con `CreateFileMappingW`; en C++
  (`agmen/shm.hpp`), en el núcleo de C++ para Python (`agmen._core.ShmPayload`)
  y en Python puro (`agmen._shm`), con los mismos bytes y nombres.
- **Se prueba si se puede, y si no, TCP.** Al conectarse, el suscriptor dice que
  puede y el publicador le pide abrir un bloque de prueba. Si no puede (otra
  computadora, otro contenedor sin `--ipc=host`, otro usuario), todo sigue por
  TCP y el suscriptor lo dice una vez: `'/camara' llega por TCP y no por
  memoria compartida: no pude abrir /agm… ¿El publicador está en otra
  computadora o en otro contenedor?`. Un nodo de 0.10 y uno de 0.11 se siguen
  entendiendo por TCP en los dos sentidos.
- **Nadie frena a nadie y la memoria tiene tope**: hasta 4 mensajes (y 64 MiB)
  prestados por suscriptor (del siguiente en adelante se copian al recibir, con
  un aviso); hasta 8 (y 128 MiB) sin devolver por conexión y luego solo el más
  nuevo (D3 del RFC 0008); hasta 64 bloques y 512 MiB por publicador
  (`AGMEN_SHM_MB`). Si no alcanza o `/dev/shm` está lleno (Docker: 64 MB), ese
  mensaje va por TCP y se avisa por qué.
- **Limpieza**: si un suscriptor muere, sus bloques vuelven al cerrarse su
  conexión; si un publicador muere, las vistas que ya se tenían siguen siendo
  válidas y el siguiente publicador que arranca borra sus bloques (registro en
  `$TMPDIR/agmen-shm-<uid>`; en Windows el sistema los borra solo). En Linux se
  reserva el bloque con `posix_fallocate`: un `/dev/shm` lleno es un aviso, no
  un `SIGBUS`.
- `pub.shm_subscribers` y `pub.shm_fallbacks` (C++ y Python);
  `envelope.flags & FLAG_SHM` dice si un mensaje llegó por memoria compartida;
  `AGMEN_SHM=0` lo apaga.
- En C++: `agmen::Payload` (bytes propios o una vista prestada) en
  `RawSample::payload`; `Subscriber<M>::receive_raw()` para leer el mensaje
  directo del bloque; `Envelope::read_from(buf, shm)`; `net::wait_rw`. En
  Python: `wire.parse(buf, shm=False)`.
- **`bench/shm_vs_tcp.py`**: la cámara y los suscriptores en procesos distintos,
  de 640×480 a 4K, con 1 y 3 suscriptores, contra el TCP de 0.10.0. Medido: 4K
  con 3 suscriptores, de 21.9 ms a 5.8 ms de latencia y de 28.9 ms a 2.6 ms de
  CPU de la cámara por imagen; 1920×1080 con 3, de 5.1 ms a 1.9 ms (tabla
  completa en el RFC 0009 §8).
- Tutorial, capítulo 15 (memoria compartida); el ejemplo `examples/vision`
  dice cuántas imágenes llegaron por memoria compartida.

### Cambiado
- Con dirección fija, el suscriptor de C++ escucha TCP en la misma IP que pidió
  para UDP (antes, siempre en `127.0.0.1`; Python ya lo hacía):
  `Subscriber(Imagen, "0.0.0.0:7500")` recibe imágenes de otra computadora.
- El `Publisher` de un tipo arrow lee lo que le dice cada suscriptor por TCP y
  se entera en el acto cuando uno se desconecta.
- Un mensaje chico ya no se adelanta a uno grande que espera en la fila de un
  suscriptor lento: lo reemplaza (D3), como siempre debió ser.
- Un sobre con `SHM` en un paquete que no lo pidió da un error claro (antes:
  "llega en el nivel 8").

### Pruebas
- `tests/test_shm.cpp` (también con ASan/UBSan) y `python/test_shm.py`, con los
  dos núcleos y cruzados (publicador en C++ y suscriptor en Python puro, y al
  revés): bytes del descriptor y los avisos, préstamos y topes, orden, un
  suscriptor que no puede abrir la memoria (como si estuviera en otra
  computadora), `AGMEN_SHM=0`, el tope de memoria, y un proceso que muere sin
  cerrar (lo que deja C++ lo limpia Python).
- `scripts/tutorial_check.py`: en el capítulo 14, el contador de C++ y la IA de
  Python deben recibir casi todas las imágenes por memoria compartida, en los
  cuatro sistemas del CI.
- `scripts/wheel_check.py` revisa que cada wheel (las 5 compiladas y la de
  Python puro) reciba una `Imagen` por memoria compartida; `scripts/py_bench.py`
  dice cuántas imágenes llegaron así.

## [0.10.1] — 2026-11-04

### Corregido
- **Los mensajes ya se pueden mandar a otro proceso con `pickle`**
  (`multiprocessing`, los workers del `DataLoader` de PyTorch). Antes fallaba
  con cualquier tipo de `agmen.load` (`import of module 'agmen_msgs...'
  failed`) y con todo mensaje recibido que trajera `bytes` (`cannot pickle
  memoryview objects`). Ahora `agmen.load` registra el paquete `agmen_msgs`, y
  los mensajes con campos variables convierten sus `memoryview` a `bytes` solo
  al hacer pickle: al recibir siguen llegando sin copiar. Con `spawn` (Windows,
  y el `DataLoader`), el proceso hijo encuentra los tipos porque vuelve a
  correr el `agmen.load` de tu script.
- `agmen.msgs.Imagen` e `ImagenComprimida` se llaman así (antes
  `agmen_msgs.msgs.Imagen`): otro proceso las encuentra con solo importar
  `agmen`.
- `agmen.Tensor` se puede pasar por `pickle` aunque sus datos sean una vista
  sin copiar.

### Pruebas
- `test_idl`: pickle de mensajes recibidos (con `bytes`, `bytes[]`, enums,
  structs y tensores, con y sin `numpy=True`) y un `Pool` de `multiprocessing`
  con `spawn`.
- `tutorial_check`, capítulo 14: espera a que el contador de C++ y la IA estén
  registrados antes de arrancar la cámara, y el contador corre hasta que la
  cámara termina (antes vivía 4 s fijos; si la cámara tardaba en arrancar, el
  contador ya se había ido). Y se compara contra lo que la cámara publicó de
  verdad (`camara.py --seconds` ahora imprime el total al terminar): en macOS
  del CI la cámara no llega a 30 fps, y eso no es culpa de quien recibe.
- `web_smoke`: el ritmo del navegador a 10 imágenes/s se mide entre su primera
  y su última imagen, sin contar lo que tardó la cámara en arrancar.
- `paralelo_check`, grupos: "solo" y "otro grupo" se corren dos veces,
  alternadas, y cuenta la mejor. En una máquina que no sostiene 1 kHz ni con
  el control solo (macOS del CI: 454 a 859 ciclos perdidos de 3000, según la
  vuelta), se exige que con la IA en otro grupo quede más cerca de "solo" que
  de "mismo grupo"; donde sí lo sostiene, sigue el 10 % de siempre.

## [0.10.0] — 2026-11-04

**Nivel 7: imágenes, tensores y texto** (RFC 0008, aceptado). Los mensajes con
campos de tamaño variable viajan con la codificación `arrow` y por TCP; los de
siempre no cambian ni un byte.

### Agregado
- **Tipos de tamaño variable en `.agmen`**: `string`, `bytes`, `T[]` y
  `tensor<T,N>` (también en esquemas escritos a mano). Un `message` con alguno
  va en la codificación **`arrow`** (RFC 0001 §6: un RecordBatch de Arrow IPC
  que `pyarrow`, Polars y DuckDB leen tal cual). Agmen implementa el subconjunto
  que usan sus tipos en C++ (`agmen::arrow`), Python (`agmen.arrow`, la
  referencia) y JavaScript (`agmen.js`), con los mismos bytes
  (`tests/vectors/v1.txt`); en el CI, `pyarrow` lee lo que escribe Agmen y al
  revés.
- **Transporte TCP** para los mensajes `arrow` (hasta 64 MiB;
  `AGMEN_MAX_MESSAGE_MB`): el `Subscriber` escucha también en un puerto TCP que
  registra en el hub (`sub ... tcp=`); el `Publisher` se conecta a cada uno y
  un hilo termina de mandar lo que no cupo de una vez. **Un suscriptor lento
  recibe solo el mensaje más nuevo** y no frena al publicador ni a los demás
  (`Publisher.dropped` cuenta los descartados). Los `compact` siguen por UDP.
- **`agmen.msgs`**: `Imagen` e `ImagenComprimida`, con los mismos campos que
  los tipos de Foxglove. `import "agmen/msgs.agmen"` en tu `.agmen`,
  `from agmen.msgs import Imagen` en Python (`Imagen.from_array()` y
  `as_array()` van y vienen de NumPy sin copiar), `<agmen/msgs.hpp>` en C++.
- **`agmen gen`** genera para los tipos arrow `to_value()`/`from_value()` y, en
  los `message`, `encode_payload`/`decode_payload` con `agmen::arrow::Codec`;
  `agmen.load(..., numpy=True)` da `T[]` y los tensores como NumPy sin copiar;
  `agmen.Tensor` para tensores sin NumPy. Valores por defecto `string s = "hola"`
  y `f32[] x = [1, 2]`.
- **Panel: tarjeta Cámara** para `Imagen` (cruda, en un canvas, 10 imágenes
  por segundo por defecto) e `ImagenComprimida` (JPEG/PNG/WebP, a 30), con
  selector de imágenes por segundo; MB/s por topic; el inspector y
  `agmen echo` resumen los campos grandes (`data: <921 600 bytes>`);
  `agmen topic hz` muestra MB/s. **Foxglove** ve `Imagen` como
  `foxglove.RawImage` e `ImagenComprimida` como `foxglove.CompressedImage`.
- Ejemplo `examples/vision` (capítulo 14): una cámara (OpenCV o un cuadro que
  se mueve), una IA en Python que la mira con NumPy y un contador en C++.
  `scripts/paralelo_check.py` mide una cámara de 640×480 a 30 fps (27 MB/s)
  hacia el panel y dos navegadores, uno que no lee, mientras un control a
  1 kHz no pierde ciclos; `scripts/py_bench.py` mide la imagen de 640×480.
- En C++: `agmen::schema::Layout` lee los tipos variables; `TcpStream`,
  `TcpListener` y `wait_io` en `agmen/net.hpp`; `Subscriber::native_handles()`
  y `tcp_port()`; `wire::is_arrow_v<M>`, `encoding_v<M>`, `max_message()`.

### Corregido
- **macOS no dejaba mandar mensajes de más de 9 KB** (`Message too long`,
  `net.inet.udp.maxdgram`): todos los sockets UDP piden `SO_SNDBUF` de
  65 507 bytes. Prueba en el CI con un `compact` de 60 KB.
- El límite de mensajes por segundo del panel (`max_hz`) dejaba pasar uno de
  cada dos cuadros de una cámara que publica exactamente a ese ritmo.

### Cambiado
- El `[[mux]]` rechaza con un aviso claro los tipos arrow: arbitra comandos
  de actuadores (`compact`).
- El puente del panel sigue en Python (RFC 0008, D6): medido con la cámara de
  27 MB/s, usa ~5 % de un núcleo y el control a 1 kHz no pierde ciclos.

## [0.9.0] — 2026-11-04

**Nivel 6: Python de primera clase** (RFC 0007, aceptado). `pip install agmen`
trae el mismo núcleo de C++ ya compilado; tu código de Python no cambia.

### Agregado
- **`agmen._core`** (nanobind): codificar y decodificar, `Publisher` y
  `Subscriber` (topics, hub, anuncios), el `Node` (temporizadores, colas y
  grupos) y el reloj de Agmen corren en C++. Medido con `Articulaciones` de
  Arca9: codificar 4.9×, decodificar 2.8×, `publish()` 4.5× y recibir 3.1×
  más rápido que Python puro (`scripts/py_bench.py`).
- **El `Node` de Python usa el executor de C++**: espera, temporizadores y colas
  sin el GIL; tus funciones toman el GIL solo mientras corren.
- **Wheels compiladas** para Windows x64, Linux x64 y ARM64, macOS Intel y
  Apple Silicon, con Python 3.11 y una sola wheel (ABI estable) para 3.12 en
  adelante (cibuildwheel; cada una se instala y se prueba antes de subirla).
  En otras computadoras, una wheel de **Python puro** que funciona igual y lo
  avisa una vez.
- **`agmen.load(..., numpy=True)`**: los arreglos de números llegan como
  `numpy.ndarray` de solo lectura que apuntan a los bytes recibidos, sin
  copiar. `pip install agmen[numpy]`.
- `agmen --version` dice qué núcleo usas: `agmen 0.9.0 (núcleo C++)` o
  `(Python puro)`. `AGMEN_BACKEND=pure|native` para elegir.
- En C++: `agmen::schema::Layout` (esquemas leídos en tiempo de ejecución),
  `PublisherCore` / `SubscriberCore` (sin plantilla), `Node::subscribe_raw` y
  `agmen::log` (a dónde van los avisos).
- `Subscriber.port` (Python): el puerto local donde escucha.
- Pruebas: todas las de Python corren con los dos núcleos en el CI;
  `python/test_core.py` compara C++ contra Python puro byte por byte;
  `tests/test_layout.cpp`; `scripts/py_bench.py` y `scripts/wheel_check.py`.
- Tutorial, capítulo 13 (Python rápido y NumPy); el capítulo 0 explica la
  instalación sin compilar.

### Cambiado
- **Python 3.11 o más nuevo** (3.10 dejó de recibir actualizaciones en octubre
  de 2026).
- El paquete se construye con scikit-build-core (antes hatchling). En el
  repositorio, CMake compila `agmen._core` en `python/agmen/` si está nanobind
  (`pip install nanobind`); si no, Python usa la versión pura.
- La cola de un `Node` guarda los mensajes sin decodificar: con `queue = 1`
  solo se decodifica el que se entrega.
- Leer de un `Subscriber` ya no se detiene antes de tiempo por un anuncio o un
  mensaje del hub: sigue leyendo lo que ya llegó.
- El código que genera `agmen.load` ya no copia los arreglos de números, y
  convierte los enums más rápido.

## [0.8.0] — 2026-11-03

**Nivel 5: un nodo con varios ritmos y el reloj de Agmen** (RFC 0006,
aceptado). Lo lento ya no frena a lo rápido dentro de un mismo nodo, y el
mismo código corre en tiempo real o simulado.

### Agregado
- **`agmen::Node` (C++) y `agmen.Node` (Python)**: `every(periodo, f)`
  programa por *deadline*; `subscribe` / `@node.on` llaman a tu función cuando
  llega un mensaje; `spin()` / `spin_for()` / `stop()`; `node.now()` es la
  hora de Agmen. `node.publisher<M>(topic)` publica con `t_data = node.now()`.
- **Grupos**: `node.group("ia")` es otro hilo. Lo del grupo principal corre
  una función a la vez (sin candados); grupos distintos corren al mismo
  tiempo. `agmen::Latest<T>`: el último valor, seguro entre hilos.
- **Ciclos saltados**: si una función tarda más que su periodo, los ciclos
  perdidos se saltan (no se recuperan de golpe), se cuentan y se avisan una
  vez cada 5 s, diciendo qué función del grupo fue la lenta (o que la
  computadora despertó tarde al nodo).
- **Colas por suscripción**: `queue = 1` (por defecto) entrega el más reciente
  y cuenta los reemplazados; `queue = N` guarda hasta N y cuenta los
  descartados.
- **Excepciones**: si tu función lanza, el nodo se detiene y `spin()` la
  relanza. Ctrl+C, SIGTERM y el CTRL_BREAK de `agmen run` lo detienen en orden.
- **El reloj de Agmen** (`agmen/clock.hpp`, `agmen.clock`): `now_ns()`,
  `mono_ns()`, modo `real` o `sim`. En simulación sigue al topic `/clock`
  (`Clock{time t;f64 speed;bool paused}`), interpolando entre mensajes, y
  nunca va hacia atrás.
- **`[clock]` en robot.toml**: `mode = "sim"`, `speed`, `paused` y `source`.
  Con `source = "agmen"`, `agmen run` publica `/clock` 50 veces por segundo;
  con el nombre de un nodo, tu simulador manda el tiempo paso a paso. Todos
  los nodos reciben el mismo reloj (`AGMEN_CLOCK`, `AGMEN_CLOCK_SOURCE`).
- **Panel**: tarjeta *Reloj* (pausa, paso, velocidad; operación `clock` del
  WebSocket) y *Ritmo de los nodos* (temporizadores y colas).
- **`agmen node list`** muestra los temporizadores y las colas de cada
  `Node`; el hub las recibe con la operación nueva `stat`.
- `examples/nodos/`: `doble_ritmo` (control a 1 kHz + IA de 80 ms) y
  `cerebro.py`; tutorial, capítulo 12.
- `scripts/node_check.py` en el CI (sim ×2, pausa desde el panel, simulador
  paso a paso, aviso de otro reloj) y la prueba 6 de `paralelo_check`:
  control a 1 kHz con la IA en otro grupo (en el contenedor de pruebas: 3 de
  3000 ciclos saltados; en el mismo grupo: 2262).

### Cambiado
- `wire::now_ns()` (C++) y `wire.now_ns()` (Python) devuelven la hora de
  Agmen; en modo real es la misma de siempre. `wire::wall_ns()` / `wire.wall_ns()`
  dan la de la computadora.
- El campo `clock` del sobre ya se llena con el modo del proceso, y quien
  recibe un mensaje de otro reloj lo avisa una vez (RFC 0001 §8.8).
- `Publisher` es seguro entre hilos (se puede publicar desde varios grupos).
- `agmen topic echo` muestra el tiempo simulado en segundos y no calcula
  latencia contra un reloj distinto.
- El hub no pide declarar `/clock` en robot.toml (es de Agmen).

## [0.7.0] — 2026-11-03

**Nivel 4: tus mensajes en un archivo `.agmen`** (RFC 0005, aceptado). Escribes
un mensaje una sola vez y Agmen genera el código de C++ y de Python. Los bytes
no cambian.

### Agregado
- **Archivos `.agmen`**: `package`, `import`, `const`, `enum` (con base
  entera), `struct`, `message`, arreglos fijos y valores por defecto. Los
  comentarios pasan al código como documentación. Errores con
  archivo:línea:columna y sugerencias (`'float' no existe en Agmen: usa
  'f32'`, `¿quisiste decir 'TROTAR'?`).
- **`agmen gen`**: escribe el `.hpp` de C++ (`--cpp`) y el `.py` de Python
  (`--python`); `--check` muestra cada tipo con su esquema, `schema_id` y
  tamaño; `--check-files` falla si los archivos generados están viejos.
- **`agmen_add_messages(<target> <archivos>...)` en CMake**: genera los
  headers al compilar y los regenera solo cuando cambias el `.agmen`. Funciona
  también desde tu propio proyecto con `add_subdirectory(agmen)`. Necesita
  Python ≥ 3.10, no `pip install`.
- **`agmen.load("arca9.agmen")`**: los tipos en Python sin compilar nada;
  ejecuta el mismo código que `agmen gen --python`.
- C++ generado: los bytes exactos del layout `compact` (relleno en 0),
  `to_string()` para los enums, un error claro si llega un valor de enum que
  no existe, y `static_assert` de que su `schema_id` es el que calculó
  `agmen gen`. En el núcleo: `agmen::wire::filled<T, N>()` y
  `WireError::Kind::BadValue`.
- `examples/mensajes/arca9.agmen` y `arca9_eco` (un perro robot de 12 servos);
  capítulo 11 del tutorial.
- Pruebas: `tests/test_gen.cpp` (el C++ generado contra los vectores),
  `python/test_idl.py` y `scripts/gen_smoke.py` (C++ generado ↔ Python) en el
  CI.

### Cambiado
- El `MotorCommand` del tutorial sale de `examples/tutorial/motor_command.agmen`
  (antes `motor_command.hpp` escrito a mano) y `cerebro.py` usa `agmen.load`.
  Capítulos 2 y 3 actualizados.

### Sin cambios
- El formato de los datos: un tipo generado y uno escrito a mano con el mismo
  esquema son el mismo tipo.

## [0.6.0] — 2026-11-03

**Nivel 3: el panel por WebSocket** (RFC 0002, aceptado). El robot le manda al
navegador los mismos bytes de Agmen que se mandan los nodos, y el navegador
los decodifica.

### Agregado
- **WebSocket** en el puerto del panel (`/ws`, protocolo `agmen.ws.v1`):
  datos en vivo, estado, logs, manejo y control de nodos por una sola
  conexión. `agmen.ws`: WebSocket (RFC 6455) con solo la librería estándar.
- **El navegador decodifica el formato v1** con `agmen.js`, la tercera
  implementación (C++, Python, JavaScript). `python/test_panel_js.py` la
  compara contra Python con los vectores de `tests/vectors/v1.txt`.
- **Submuestreo por cliente** (`max_hz`): cada pestaña pide lo suyo. Colas
  acotadas por conexión: un navegador lento pierde datos, nunca frena al
  robot (se cuentan en `state.bridge.dropped`).
- Panel: **varios topics graficados a la vez**, **inspector** del último
  mensaje (campo por campo, con `seq`, emisor, hora y tamaño; se puede
  congelar) y la tarjeta **"CPU del panel"**.
- **Modo red local con clave:** `agmen run --red` / `agmen web --red`
  imprime el link `http://IP:8080/?t=…`; sin la clave, 401. Clave fija con
  `[web] token` o `--token`.
- **Hombre muerto en el puente:** si el navegador estaba manejando y deja de
  mandar comandos 300 ms (se cortó la señal) o se cierra la conexión, el
  panel manda ALTO y lo dice en los logs.
- **Compatible con Foxglove** (`foxglove.websocket.v1`, solo lectura): cada
  topic es un canal JSON con su JSON Schema (`schema.Layout.json_schema()`).
  Probado con el cliente oficial `@foxglove/ws-protocol`
  (`scripts/foxglove_check.py`, en el CI). `[web] allow_origins` para otros
  visores.
- `examples/tutorial/mirar_panel.py` y el capítulo 10 del tutorial.
- `scripts/web_smoke.py` mide el costo del puente: ~20 % de un núcleo
  reenviando 1000 mensajes/s a 3 navegadores; unos pocos % a 100 Hz.

### Cambiado
- El panel ya no decodifica en Python: solo revisa el sobre y reenvía los
  bytes.
- `/api/state`: las estadísticas van por topic (`stats.topics`) y agrega
  `cpu_s` y `bridge`.

### Quitado
- `/api/stream` (Server-Sent Events): lo reemplaza el WebSocket. `/api/state`
  y los `POST` siguen para scripts.

### Decidido
- RFC 0002 aceptado: bytes al navegador, puente en Python (en C++ en el
  nivel 7, con las cámaras), clave en red, Foxglove en solo lectura. Nivel
  14 nuevo en el roadmap: un visualizador propio, para cuando Agmen esté
  completo.

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
- El publisher de ejemplo reporta al final su peor atraso contra el horario
  (`atraso_max=… ms`). `topics_smoke` revisa ese número (< 500 ms) en vez de
  exigir `retrasos=0`, que en las máquinas virtuales de macOS del CI es
  imposible: su `sleep` despierta tarde seguido aunque nada se atrase.

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
