# RFC 0010 — Grabar, reproducir y exportar: tu robot como dataset (nivel 9)

| | |
|---|---|
| **Estado** | ✅ **Aceptado** (2026-11-05, D1–D9 = a) e **implementado en Agmen 0.12.0** ([sección 8](#8-lo-que-se-implementó-en-0120)). Decisiones en la [sección 5](#5-decisiones) |
| **Depende de** | RFC 0001 (el sobre: `t_data`, `t_pub`, `source_id`, `seq` y el campo `clock`; pregunta abierta 1 de §13), RFC 0002 (panel y Foxglove), RFC 0004 (hub y topics), RFC 0006 (el reloj de Agmen y el modo `replay`), RFC 0008 (`arrow`, `agmen.msgs.Imagen`, D3), RFC 0009 (memoria compartida) |
| **Versión** | Agmen 0.12.0 |
| **Requisitos** | c (todo mensaje con marca de tiempo; las grabaciones sirven como datasets), d (misma interfaz para simulación, robot real y grabación), h (interfaz web), i (un solo comando) |

## 1. El problema

### 1.1 Lo que pasa en el robot se pierde

Hoy Agmen mueve mensajes y los olvida. No hay manera de:

- **guardar** lo que vieron la cámara, la IMU y los motores durante una
  prueba, para entrenar una red (el requisito c lo pide desde el día 0);
- **volver a ver** qué pasó cuando el robot hizo algo raro, cuadro por cuadro
  y con las gráficas del panel;
- **repetir** una prueba sin el robot: mandarle las mismas imágenes y
  lecturas a una versión nueva de tu IA y comparar lo que decide (dora-rs lo
  llama *sustituir nodos*; sirve como prueba de regresión).

El RFC 0001 dejó listo lo que esto necesita: dos marcas de tiempo en cada
mensaje, `source_id` + `seq` para ver huecos, esquemas que viajan con los
datos y el campo `clock` del sobre con el valor `2` = reproducción. También
dejó abierta una pregunta (§13.1): ¿MCAP o un formato propio sobre Arrow IPC?
El RFC 0006 dejó el modo de reloj `replay` para este nivel.

### 1.2 Lo que se quiere

- **Un comando** graba todos los topics, o los que digas, sin cambiar ni una
  línea de los nodos.
- **Nunca frena a nadie** (regla 12): si el disco no alcanza, se pierden
  mensajes de la grabación, no ciclos del control. Y **nunca en silencio**
  (regla 1): lo que se perdió se cuenta, se avisa y queda escrito en el
  archivo.
- **Reproducir** con el tiempo original: los nodos ven la hora de la
  grabación (`node.now()`), a ×2, ×0.5 o en pausa desde el panel, igual que
  en simulación (RFC 0006).
- **Exportar** a Parquet para entrenar (pandas, PyTorch, Hugging Face), con
  columnas que tengan su tipo (`t_data` como fecha y hora en ns, una imagen
  como binario…).
- **Ver** la grabación en el panel, con una barra de tiempo, sin que el robot
  haga nada; y también en Foxglove.
- Que si el grabador **se cae** (o se va la luz), lo grabado hasta ese
  momento se pueda leer.
- Un formato **abierto**: que otras herramientas lo abran sin Agmen.

## 2. Cómo lo resuelven otros

| Sistema | Cómo | Qué se aprende |
|---|---|---|
| **ROS 2** (`ros2 bag`) | Desde Iron (mayo de 2023) graba en **MCAP** por defecto; antes, en SQLite3, que sigue disponible. `ros2 bag record` / `play`, `--clock` para que los nodos vean el tiempo de la grabación, `--rate`, dividir por tamaño, compresión zstd. Un caché en memoria (`--max-cache-size`, con doble buffer) absorbe al disco | Si el disco no alcanza, el caché crece o se pierden mensajes, y **no queda dicho en el archivo**: los usuarios comparan a mano lo que dice `ros2 bag info` con lo que esperaban (hay una propuesta para guardar las pérdidas en los metadatos). Lo que se toma: MCAP, `--clock`, `--rate` |
| **MCAP** (abierto, de Foxglove) | Un contenedor de mensajes con tiempo: registros `Schema`, `Channel` (un topic) y `Message` (`log_time`, `publish_time`, bytes), agrupados en *chunks* (opcionalmente con lz4 o zstd), con índices y un resumen al final. Acepta **codificaciones propias**, no solo las registradas (ROS 1, CDR, Protobuf, FlatBuffers, JSON…). Hay bibliotecas en C++, Python (licencia MIT), Go, Rust y TypeScript, y la herramienta `mcap` (`info`, `cat`, `doctor`, `recover`, `filter`, `merge`) | Un archivo cortado (sin resumen) se lee de principio a fin, y `mcap recover` lo arregla. La biblioteca de Python pide `lz4` y `zstandard` aunque no comprimas |
| **dora-rs** | `dora record` mete un nodo grabador en el flujo y guarda todo, o lo filtrado, en un archivo `.drec`. `dora replay` reemplaza a los nodos que produjeron esos datos por nodos que los reproducen, a la velocidad que digas. Antes, un nodo `dora-record` (experimental) escribía un Parquet por entrada | **Reproducir sustituyendo nodos** (prueba de regresión). Su formato es propio |
| **LeRobot** (Hugging Face) | El formato de datasets para robots que aprenden. La v3.0 junta muchos episodios por archivo: **Parquet** para lo numérico, **MP4** por cámara para las imágenes, y metadatos (`meta/info.json` con el esquema y los fps; los episodios, en Parquet) | El destino de los datasets: Parquet por columnas. El video necesita un codificador (ffmpeg), que no es para el núcleo |
| **Rerun** | `.rrd`: mensajes uno tras otro, cada uno con un *chunk* que es un RecordBatch de **Arrow IPC** | Arrow por dentro funciona, pero sus archivos todavía no son compatibles entre versiones: un formato propio es una promesa que hay que cumplir para siempre |
| **Arrow IPC y Parquet** | Arrow tiene un formato de *stream* (se lee de principio a fin) y uno de archivo (Feather v2) con un pie al final para saltar a cualquier parte; Parquet también guarda su índice al final | Un archivo de Arrow o un Parquet **sin su pie no se puede leer**: sirven para exportar, no para grabar mientras el robot corre |

Lo que se toma: de ROS 2, **MCAP**, `--clock` y `--rate`, y la lección de que
las pérdidas deben quedar escritas; de dora, **reproducir sustituyendo
nodos**; de LeRobot, **Parquet** como destino de los datasets; de Arrow y
Parquet, que sirven para exportar, no para grabar en vivo.

## 3. Propuesta

### 3.1 La idea en un dibujo

```
 robot (o simulador)                              después, en tu laptop
 ───────────────────                              ─────────────────────
 cámara ─┐                                        agmen play vuelta1.mcap ──► tu IA nueva
 imu ────┼─► topics ─► agmen record ─► vuelta1.mcap ─► agmen export --parquet ──► pandas, PyTorch
 motor ──┘    (hub)    (un suscriptor más)    │
                                              ├─► panel: abrirla y verla, con barra de tiempo
                                              └─► agmen export --foxglove ──► Foxglove
```

- `agmen record` es **un suscriptor más**: el hub lo trata como suscriptor
  de todos los topics, igual que al panel (RFC 0004). Ningún nodo se entera.
- El archivo es **MCAP**, y adentro van los **mensajes de Agmen tal como
  llegaron** (sobre de 64 bytes + payload): el mismo decodificador de
  siempre lee una grabación.

### 3.2 Grabar

```bash
agmen record                                  # todos los topics → agmen-2026-11-05_15-30-12.mcap
agmen record -o vuelta1.mcap /camara /imu     # solo esos dos
agmen run robot.toml --record                 # todo el robot, y graba mientras corre
```

O en `robot.toml`:

```toml
[record]
topics = ["/camara", "/imu", "/cmd"]   # o "*" (todos)
dir = "grabaciones"
split = "1GiB"                          # un archivo nuevo cada 1 GiB (o "10min")
```

Y en el panel, un botón **⏺ Grabar**, que hace lo mismo.

Mientras graba, dice cuánto lleva y si pierde algo:

```
[agmen] ⏺ vuelta1.mcap: 38 MB, 12 s · /camara 30/s, /imu 1000/s, /cmd 50/s
[agmen] ⚠ grabación: se perdieron 37 imágenes de /camara (el disco escribe 21 MB/s y llegan 28 MB/s)
```

`agmen info vuelta1.mcap` resume una grabación: duración, topics, tipos,
cuántos mensajes y cuántos se perdieron.

### 3.3 El archivo

| En MCAP | Qué guarda Agmen |
|---|---|
| `Header` | `profile = "agmen"`, `library = "agmen 0.12.0"` |
| `Schema` | `name` = el nombre del tipo (`Imagen`), `encoding = "agmen"`, `data` = el esquema canónico (`agmen/1 Imagen{…}`), el mismo texto que viaja en el anuncio (RFC 0001) |
| `Channel` | uno por topic: `topic = "/camara"`, `message_encoding = "agmen"`; en sus metadatos, el `schema_id` y la codificación (`compact` o `arrow`) |
| `Message` | `data` = **el paquete de Agmen completo** (sobre + payload); `publish_time` = `t_pub`; `log_time` = cuándo lo recibió el grabador; `sequence` = los 32 bits bajos de `seq` |
| `Metadata` | `agmen.grabacion`: versión de Agmen, computadora, modo del reloj, comando y robot; al cerrar, `agmen.perdidos`: cuántos mensajes se perdieron por topic y por qué |
| `Attachment` | si se graba con `agmen run`, el `robot.toml` y los `.agmen` del robot: la grabación se explica sola |
| *Chunks* | de unos 4 MiB o 1 s (lo que llegue primero), cada uno con su índice: si el grabador se cae, se pierde como mucho el último segundo |

- Lo que llegó por **memoria compartida** se guarda con su payload de verdad
  (no con el descriptor de 72 bytes) y sin la bandera `SHM`: queda igual que
  si hubiera llegado por TCP.
- **Sin dependencias**: Agmen trae su propio lector y escritor de MCAP (unas
  300 líneas en Python y otras tantas en JavaScript para el panel). En el CI,
  la biblioteca oficial de MCAP revisa que cada archivo esté bien escrito,
  igual que `pyarrow` revisa la codificación `arrow` desde 0.10.0.
- El perfil `agmen` y la codificación `agmen` se documentan en
  `docs/PROTOCOLO.md`, para que cualquiera escriba su propio lector.

### 3.4 Reproducir

```bash
agmen play vuelta1.mcap                     # todos los topics, cada mensaje a su tiempo
agmen play vuelta1.mcap --rate 2 --loop     # al doble, una y otra vez
agmen play vuelta1.mcap /camara /imu        # solo los sensores: tu IA nueva calcula /cmd en vivo
agmen run robot.toml --play vuelta1.mcap    # el robot entero, con la grabación en vez de los sensores
```

- Cada mensaje sale **cuando le toca**: en el orden y con los intervalos con
  que llegó al grabador (`log_time`), multiplicados por `--rate`. Sale por su
  topic y con **los mismos bytes**, salvo el campo `clock` del sobre, que
  dice `replay` (2). `t_data`, `t_pub`, `source_id` y `seq` son los de la
  grabación.
- **El tiempo de la grabación manda** (RFC 0006, modo `replay`): `agmen play`
  publica `/clock` con la hora de la grabación. Así `node.now()`, `every()` y
  el `t_data` de lo que publican tus nodos están en el tiempo de la
  grabación, y la pausa y la velocidad se manejan desde el panel, como en
  simulación. Con `agmen run --play`, todos los nodos arrancan en modo
  `replay`: no hay forma de olvidar uno. Y si un nodo en reloj real recibe
  un mensaje de la grabación, lo avisa (RFC 0001 §8.8).
- **Sustituir nodos** (como dora): reproduces solo `/camara` e `/imu`, corres
  tu IA nueva y comparas su `/cmd` con el `/cmd` grabado.

### 3.5 Exportar para entrenar

```bash
agmen export vuelta1.mcap --parquet vuelta1/    # vuelta1/camara.parquet, vuelta1/imu.parquet, …
```

```python
import agmen

rec = agmen.Recording("vuelta1.mcap")
rec.topics                         # {"/camara": Imagen, "/imu": ImuSample, …}
for s in rec.messages("/imu"):     # un agmen.Sample, como los de Subscriber.receive()
    print(s.envelope.t_data, s.message.accel)
tabla = rec.table("/imu")          # pyarrow.Table: t_data, t_pub, log_time, source_id, seq, ax, ay, …
df = tabla.to_pandas()
```

- **Una tabla por topic**, una fila por mensaje: `t_data` y `t_pub` como
  `timestamp[ns]`, `log_time`, `source_id`, `seq`, y los campos del esquema
  con su tipo de Arrow, según el mapeo de los RFC 0001 y 0008. Un mensaje
  `arrow` ya es un RecordBatch, así que se pega tal cual; uno `compact` se
  convierte campo por campo.
- `bytes` (una imagen, un JPEG) queda como columna binaria; `tensor<T,N>`,
  como lista de largo fijo, con su forma en los metadatos de la columna.
- Parquet necesita `pyarrow` (`pip install agmen[datasets]`). Leer la
  grabación en Python (`rec.messages`) no necesita nada.
- El formato **LeRobot** (Parquet + MP4, por episodios) se arma encima de
  esto en el nivel 13 (`agmen-learn`): necesita un codificador de video, y
  eso no es para el núcleo.

### 3.6 Ver la grabación: en el panel y en Foxglove

- **En el panel**: abres el `.mcap` (arrastrándolo al panel, o con
  `agmen web vuelta1.mcap`) y el navegador lo lee y lo decodifica con
  `agmen.js`, el mismo lector de los mensajes en vivo. Tienes gráficas,
  cámara, inspector y una barra de tiempo (reproducir, pausa, velocidad,
  saltar). No hay robot en medio: todo pasa en tu navegador, que lee el
  archivo por partes (con el índice de MCAP) aunque mida varios GB.
- **En Foxglove**: Foxglove abre MCAP, pero solo decodifica las codificaciones
  que trae (JSON, Protobuf, FlatBuffers, ROS…), no la de Agmen, y sus
  extensiones convierten mensajes que ya decodificó, de un esquema a otro.
  Por eso `agmen export vuelta1.mcap --foxglove vuelta1-foxglove.mcap`
  escribe otro MCAP con los mensajes en JSON y las imágenes como
  `foxglove.RawImage` y `foxglove.CompressedImage`. Es lo mismo que el puente
  del panel le manda a Foxglove en vivo (JSON desde 0.6.0; imágenes desde
  0.10.0).

## 4. Lo que NO entra en 0.12.0

- Grabar **desde otra computadora**: los topics todavía son de una sola
  máquina (nivel 11).
- Una API de **C++** para leer y escribir grabaciones. El grabador y el
  lector son de Python, y cualquier biblioteca de MCAP de C++ lee los
  archivos; se agrega cuando alguien la necesite.
- Convertir imágenes a JPEG o a video al grabar. Si el disco no alcanza,
  publica `ImagenComprimida` (ver §6).
- El formato LeRobot (nivel 13), y reproducir "lo más rápido posible" sin
  perder mensajes, con confirmación de cada nodo.
- Recortar, juntar o filtrar grabaciones: `mcap filter` y `mcap merge` ya lo
  hacen con cualquier codificación.

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿En qué formato se graba? | (a) **MCAP, con los mensajes de Agmen adentro** (perfil `agmen`, codificación propia), (b) uno propio sobre Arrow IPC (un archivo por topic, ya en columnas), (c) uno propio y simple (los paquetes uno tras otro, con un índice al final), (d) el de ROS 2 con SQLite3 | **(a)** | MCAP ya resuelve lo difícil (índices, *chunks*, compresión, archivos cortados) y es el formato de ROS 2 y de Foxglove: otras herramientas lo abren, lo recortan y lo juntan. (b) no tiene orden entre topics ni índice de tiempo, obliga a convertir los mensajes `compact` en el robot (CPU) y, con pie, no se lee si el grabador se cae. (c) es reinventar MCAP sin sus herramientas. (d) ata a SQLite y a ROS |
| **D2** | ¿Qué se guarda de cada mensaje? | (a) **El paquete de Agmen completo, sin cambiar un byte** (sobre + payload; lo de memoria compartida, con su payload de verdad), (b) solo el payload, y del sobre lo que quepa en el registro de MCAP, (c) el mensaje convertido a JSON o CBOR | **(a)** | Con (a), reproducir es mandar los mismos bytes, y cualquier lector de Agmen (C++, Python, `agmen.js`) entiende la grabación sin código nuevo. El sobre cuesta 64 bytes: nada junto a una imagen, y unos 64 KB/s en una IMU a 1 kHz. (b) pierde `t_data`, `source_id`, el `seq` de 64 bits y `clock`, que el registro de MCAP no tiene. (c) infla las imágenes (base64) y deja de ser Agmen |
| **D3** | ¿Quién graba? | (a) **`agmen record`: un proceso aparte, suscriptor de todos los topics (o de los que digas), que recibe por memoria compartida cuando puede**; también `agmen run --record`, `[record]` en `robot.toml` y un botón en el panel, (b) el hub, que ya conoce todos los topics, (c) cada nodo graba lo suyo, con una biblioteca adentro del nodo | **(a)** | (a) no toca ningún nodo y aprovecha lo que ya existe: el hub le avisa de cada topic, y la memoria compartida le ahorra copias (de la memoria de la cámara al archivo, una sola). (b) metería datos en el hub, que solo es de control (RFC 0004): si el hub se atrasa, se atrasan todos. (c) obliga a cambiar cada nodo, y juntar sus archivos es otro problema |
| **D4** | ¿Qué pasa si el disco no alcanza? | (a) **El grabador nunca frena a nadie: una cola con tope (unos segundos de datos); si se llena, descarta, lo cuenta por topic, lo avisa y lo deja escrito en el archivo**, (b) frenar a los publicadores hasta que el disco escriba, (c) una cola sin tope | **(a)** | Es la regla 12 (nadie espera a nadie) junto con la regla 1 (nunca en silencio): un dataset con huecos se nota en `agmen info` y en el Parquet (la columna `seq`), en vez de descubrirlo al entrenar. Los huecos de la red (`seq` por `source_id`) se cuentan igual. (b) haría que una microSD lenta frene el control del robot. (c) acaba con la RAM, el sistema mata al grabador y se pierde todo lo que estaba en la cola |
| **D5** | ¿Se comprime? | (a) **No por defecto; `--compress zstd` o `lz4` para quien lo pida** (necesita `pip install agmen[grabar]`), (b) siempre zstd, (c) nunca | **(a)** | Comprimir cuesta CPU en el robot, y lo que más pesa (las imágenes en bruto) se reduce poco con zstd o lz4; para la cámara, lo que sirve es publicar `ImagenComprimida` (JPEG). (b) le quita CPU a una Raspberry Pi. (c) le niega a quien graba números (IMU, motores) una ganancia casi gratis, porque se comprimen muy bien |
| **D6** | ¿Cómo se reproduce? | (a) **`agmen play` manda los mismos bytes a su tiempo (×`--rate`), con el campo `clock` = `replay`, y publica `/clock` con la hora de la grabación: los nodos en modo `replay` (RFC 0006) ven el tiempo de la grabación**; con `agmen run --play`, todos los nodos en ese modo, (b) mandar los mensajes con la hora de ahora (`t_pub` y `t_data` nuevos), (c) solo leer la grabación desde Python, sin reproductor | **(a)** | Con (a), tu nodo no distingue una grabación del robot real (requisito d), los tiempos de la grabación siguen teniendo sentido (`t_data`, latencias) y la pausa y la velocidad son las de la simulación, que ya existen. (b) borra cuándo se capturó cada dato, y con eso la sincronía entre sensores. (c) obliga a cada quien a escribir su reproductor |
| **D7** | ¿Cómo se usa para entrenar? | (a) **`agmen export --parquet` (una tabla por topic, con columnas de Arrow) y `agmen.Recording` para leer en Python**; LeRobot después, encima de esto, (b) exportar directo al formato LeRobot (Parquet + MP4), (c) solo leer la grabación en Python | **(a)** | Parquet lo abren pandas, Polars, DuckDB, PyTorch y Hugging Face, y el mapeo a Arrow ya existe (RFC 0001 y 0008). (b) mete un codificador de video y decisiones de entrenamiento (episodios, tareas) en el núcleo; va en `agmen-learn` (nivel 13). (c) deja fuera a quien trabaja con tablas |
| **D8** | ¿Cómo se ve una grabación? | (a) **En el panel, en el navegador (lee el MCAP y lo decodifica con `agmen.js`, con barra de tiempo); y para Foxglove, `agmen export --foxglove`, que convierte a JSON como el puente en vivo**, (b) grabar desde el principio en JSON, para que Foxglove lo abra directo, (c) solo Foxglove, sin el panel | **(a)** | El panel ya decodifica Agmen en el navegador (desde 0.6.0): una grabación es lo mismo, leído de un archivo en vez de un WebSocket, y el robot no trabaja. Foxglove no decodifica codificaciones propias, así que para él se convierte, como ya se hace en vivo. (b) infla el archivo y pierde los bytes de Agmen (D2). (c) deja fuera lo que el panel sí sabe (el reloj, los nodos, manejar) |
| **D9** | ¿Qué pasa si el grabador se cae a la mitad? | (a) **Escribe en *chunks* de unos 4 MiB o 1 s, cada uno completo en el disco antes del siguiente: se pierde como mucho el último segundo, y el archivo se lee aunque le falte el resumen** (`agmen info --reparar` lo vuelve a escribir), (b) cada mensaje suelto, sin chunks, (c) todo en memoria, y se escribe al final | **(a)** | (a) es el término medio de MCAP: índice y compresión por chunk, y poco que perder. (b) casi no pierde nada, pero el archivo queda sin índice (el panel tendría que leerlo completo para saltar) y sin compresión. (c) pierde todo en un corte de luz |

## 6. Las partes difíciles

| # | Reto | Por qué es difícil | Cómo se enfrenta |
|---|---|---|---|
| 1 | **El disco es más lento que los datos** | Una cámara de 640×480 a 30 fps son 28 MB/s; una de 1920×1080, 187 MB/s; una 4K, 746 MB/s. La microSD de una Raspberry Pi escribe del orden de decenas de MB/s, y además el sistema le da a veces pausas largas | La cola con tope y el conteo de D4; medirlo en `bench/grabar.py` (MB/s sostenidos, CPU y pérdidas a 640×480, 1080p y 4K); y decirlo en el tutorial: para grabar la cámara en el robot, publica `ImagenComprimida` (un JPEG de 1080p mide cientos de KB, no 6 MB) |
| 2 | **Tres relojes en cada mensaje** | `t_data` (cuándo se capturó), `t_pub` (cuándo se publicó) y `log_time` (cuándo llegó al grabador), de varios publicadores, cada uno con su propio `seq` | Se guardan los tres; se reproduce por `log_time` (el orden en que el robot de verdad vio los mensajes); el Parquet trae los tres para que tú elijas con cuál alinear |
| 3 | **El reloj nunca va hacia atrás** (RFC 0006) | Saltar hacia atrás en la reproducción contradice a los nodos, que ya vieron un tiempo más adelantado | Saltar hacia adelante sí se puede: el reloj brinca. Para regresar, se reinicia la reproducción y, con `agmen run --play`, los nodos (un botón). En el panel, sin nodos, saltar a cualquier punto es libre |
| 4 | **El grabador se cae a la mitad** | Un MCAP sin resumen no tiene índice, y el último chunk puede quedar cortado | D9: se lee de principio a fin hasta el último chunk completo, y `agmen info --reparar` (o `mcap recover`) reescribe el índice. Hay una prueba que corta el archivo en todos los puntos posibles |
| 5 | **Archivos de varios GB en el navegador** | No caben en la memoria de una pestaña | El panel lee por partes (`File.slice`) con el índice de chunks de MCAP, y cada imagen se lee cuando hace falta |
| 6 | **Dos lectores y un escritor propios** | Python y JavaScript deben leer igual que la biblioteca oficial, sin dependencias | El juez: la biblioteca `mcap` oficial lee en el CI todo lo que Agmen escribe, y los lectores de Agmen leen archivos escritos por ella. Un `.mcap` chico se agrega a `tests/vectors/` |
| 7 | **Columnas con el tipo correcto** | `time`, enums, `bool`, structs anidados y tensores deben quedar en Parquet con su tipo de Arrow, no como texto | El mapeo canónico de los RFC 0001 y 0008; `pyarrow` revisa cada columna en las pruebas |

## 7. Plan para Agmen 0.12.0 (si se acepta con las recomendadas)

1. `agmen._mcap`: lector y escritor de MCAP en Python puro, probados contra
   la biblioteca oficial.
2. `agmen record`, `[record]` en `robot.toml`, `agmen run --record` y el
   botón del panel; con la cola y el conteo de pérdidas (D4) y los chunks
   (D9). `agmen info`.
3. El modo `replay` del reloj en C++ y en Python (lo que dejó listo el RFC
   0006), `agmen play` y `agmen run --play`.
4. `agmen.Recording` para leer en Python y `agmen export --parquet`.
5. El panel: abrir y reproducir un `.mcap` (lector de MCAP en JavaScript y
   barra de tiempo).
6. `agmen export --foxglove`.
7. Pruebas, con los dos núcleos de Python:
   - grabar → leer → los mismos bytes, también lo que llegó por memoria
     compartida;
   - lo que escribe Agmen lo lee la biblioteca oficial, y al revés;
   - un archivo cortado en cualquier punto se lee hasta el último chunk
     completo;
   - un disco lento (simulado): las pérdidas se cuentan, se avisan y quedan
     en el archivo;
   - reproducir: los tiempos (con margen), el reloj `replay` y el aviso al
     nodo que está en reloj real;
   - el Parquet, revisado con `pyarrow`; el MCAP para Foxglove, con la
     biblioteca oficial;
   - el lector de JavaScript, en `python/test_panel_js.py`.
8. Medición: `bench/grabar.py`.
9. Tutorial, capítulo 16: "Grabar y reproducir: tu robot como dataset".
   README, VISION, PROTOCOLO (el perfil `agmen` de MCAP) y CHANGELOG.

## 8. Lo que se implementó en 0.12.0

| Pieza | Dónde |
|---|---|
| Lector y escritor de MCAP sin dependencias (D1, D9): chunks con su índice y CRC, resumen, metadatos y adjuntos; un archivo sin resumen se lee de corrido hasta el último chunk completo; `repair()` | `python/agmen/_mcap.py` |
| `agmen record` y `agmen info` (D2–D4): un suscriptor más (por memoria compartida cuando puede), la cola con tope, las pérdidas por topic y motivo en `agmen.perdidos`, `--split`, `--compress`, `--reparar` | `python/agmen/record.py` |
| El reloj `replay` (D6) en C++ y en Python, y el aviso al nodo que recibe otro reloj | `include/agmen/clock.hpp`, `src/clock.cpp`, `src/node.cpp`, `src/pubsub.cpp`; `python/agmen/clock.py`, `python/src/core.cpp` |
| `agmen play` (D6): los mismos bytes con `clock` = replay, `/clock` con la hora de la grabación, `--rate`, `--loop`, `--start`, `--end` | `python/agmen/play.py` |
| `agmen run --record`, `[record]`, `--play`, `--play-topic`, `--skip`; el botón ⏺ del panel (`RecordingControl`) | `python/agmen/cli.py`, `run.py`, `record.py`, `panel/server.py` |
| `agmen.Recording` y `agmen export --parquet` (D7) | `python/agmen/recording.py` |
| `agmen export --foxglove` (D8): el JSON del puente en vivo (`foxglove_schema`, `foxglove_row`) | `python/agmen/recording.py`, `panel/bridge.py` |
| El panel abre grabaciones (D8): `mcap.js` lee por partes con el índice; barra de tiempo; `agmen web archivo.mcap` (con Range, y los chunks de zstd descomprimidos) | `python/agmen/panel/mcap.js`, `index.html`, `server.py` |
| El vector de prueba de §6.6 | `tests/vectors/grabacion.mcap` y `generar_mcap.py` |
| Pruebas | `python/test_mcap.py` (la biblioteca oficial, en los dos sentidos; cortar en cada byte; dañar al azar), `test_record.py`, `test_play.py`, `test_recording.py` (pyarrow y la biblioteca oficial como jueces), `test_panel_js.py` (`mcap.js` contra Python: el vector, lz4, cortadas, 60 dañadas), `test_web.py`, `tests/test_node.cpp` (reloj replay); `scripts/grabar_smoke.py` (de punta a punta con `agmen run`) y el capítulo 16 en `scripts/tutorial_check.py`, todo en el CI de los 3 sistemas y con los dos núcleos de Python |
| Medición | `bench/grabar.py` |
| Tutorial | Capítulo 16; `examples/tutorial/dataset.py` |

### Medido

`python bench/grabar.py` en el contenedor de desarrollo (Linux, 4 núcleos,
Python 3.13, núcleo de C++, el disco de la máquina). La cámara (RGB a 30 fps,
5 s) y el grabador son procesos distintos; el grabador recibe por memoria
compartida.

| Imagen | MB | Llegan | Grabadas | Se escriben | Perdidas | CPU del grabador por imagen | `publish()` de la cámara, mediana / p99 |
|---|---:|---:|---:|---:|---:|---:|---|
| 640×480 | 0.9 | 28 MB/s | 150 de 150 | 28 MB/s | 0 | 1.6 ms | 0.22 / 0.45 ms |
| 1280×720 | 2.8 | 83 MB/s | 150 de 150 | 83 MB/s | 0 | 10.6 ms | 0.45 / 1.08 ms |
| 1920×1080 | 6.2 | 187 MB/s | 150 de 150 | 187 MB/s | 0 | 23.1 ms | 1.01 / 2.84 ms |
| 3840×2160 | 24.9 | 747 MB/s | 55 de 150 | 274 MB/s | 95 (disco) | 139.9 ms | 3.82 / 18.47 ms |

Lo que dice la tabla:

- Hasta **1080p a 30 fps (187 MB/s) se graba todo**. Una cámara 4K a 30 fps
  son 747 MB/s: no cabe, el grabador descarta (y lo cuenta, lo avisa y lo deja
  escrito), y **la cámara no se frena**: `publish()` nunca espera al
  grabador. Su p99 sube porque el grabador tiene bloques de memoria
  compartida prestados mientras esperan en la cola (la cámara usa otros,
  nuevos) y por la competencia por los 4 núcleos.
- En esta máquina, escribir cuesta: un `write()` de Python solo, sin Agmen,
  gasta **4.6 ms de CPU por MB** (el kernel, en una máquina virtual). Eso es
  casi toda la CPU del grabador; lo de Agmen encima (CRC, juntar el chunk) es
  menos de 1 ms por MB. En una computadora sin virtualizar, escribir es varias
  veces más barato; en la microSD de una Raspberry Pi, el disco manda (decenas
  de MB/s). Por eso el tutorial dice: para grabar la cámara en un robot chico,
  publica `ImagenComprimida`.
- Los números cambian de una corrida a otra (±30 %); lo que no cambia es
  que nada se pierde sin contarse, y que la cámara sigue a su ritmo.

### Reglas que se fijaron al implementarlo

- **`log_time` es la hora del reloj del grabador** (real, sim o replay), no la
  de la computadora: así una simulación o una reproducción grabada se vuelve a
  reproducir con su propio tiempo. Si un topic llega con otro reloj (un robot
  en simulación y `agmen record` en reloj real), el grabador lo avisa una vez
  (`corre el grabador con AGMEN_CLOCK=sim`).
- **`agmen run` detiene los nodos antes de cerrar la grabación**: se guarda
  hasta el último mensaje, y el resumen sale antes del "listo".
- **El `/clock` grabado no se reproduce**: el tiempo lo manda el reproductor.
  Al terminar (sin `--loop`) el reloj queda **en pausa** donde acabó la
  grabación, para que los nodos no sigan en un tiempo que ya no tiene datos.
- **`--loop` y el reloj que nunca regresa**: cada vuelta le suma a `t_data` y
  `t_pub` lo que dura la grabación (más un hueco de 1/1000), y el reloj salta
  hacia adelante. Los mensajes de la segunda vuelta ya no son byte por byte
  los grabados (cambian esos dos campos).
- **El reproductor publica con el `source_id` grabado desde que nace**: en el
  CI de Windows, un suscriptor alcanzó a conectarse antes de que se cambiara,
  y los bloques de memoria compartida tenían otro dueño (el suscriptor los
  rechazó, con razón, RFC 0009). De ahí también: en Windows, crear un bloque
  cuyo nombre ya existe es un error (antes abría el ajeno), como en C++.
- **Un paquete con N filas** (RFC 0001 §6) da N mensajes en
  `Recording.messages()` y N filas en la tabla, con el mismo sobre.
- **Las columnas del sobre** se llaman `t_data`, `t_pub`, `log_time`,
  `source_id` y `seq`; si un campo del mensaje se llama igual, la del sobre
  lleva `_` adelante (`_seq`).
- **El panel, viendo una grabación, no maneja el robot**: los datos no son de
  ahora. La tarjeta *Manejar* (y el reloj del robot) se esconden hasta que la
  cierras.
- **El panel no trae zstd**: los chunks sin comprimir o con lz4 los lee el
  navegador (lz4 en unas 50 líneas); los de zstd, solo con
  `agmen web archivo.mcap`, que los descomprime en el servidor.

### Diferencias con la propuesta

- **Tensores en Parquet** (§3.5 decía "lista de largo fijo con su forma en los
  metadatos"): se usa el mapeo canónico del RFC 0008 que ya tenía
  `msg.to_arrow()`: un arreglo fijo de números de varias dimensiones va como
  `fixed_shape_tensor` (la forma va en el tipo), y un `tensor<T,N>` (forma
  distinta en cada mensaje) como `variable_shape_tensor`, o `struct{data,
  shape}` si tu pyarrow no lo tiene. Una lista de largo fijo no sirve para
  formas que cambian.
- **Adjuntos**: solo el `robot.toml`, no los `.agmen` (§3.3): los esquemas ya
  van en los registros `Schema` de MCAP.
- **El botón para volver al principio** con `agmen run --play` (§6, reto 3) no
  entró: para regresar, se detiene y se vuelve a correr. En el panel, sin
  nodos, sí se salta a cualquier punto.
- **Tamaño**: el lector y escritor de Python son unas 760 líneas (con la
  reparación, la compresión y los mensajes de error en español) y el lector de
  JavaScript unas 380, no 300 cada uno.
- Lo demás, como se propuso.

## Fuentes

- MCAP como formato por defecto de ROS 2 (desde Iron): <https://foxglove.dev/blog/mcap-as-the-ros2-default-bag-format> y el cambio en rosbag2 ("set MCAP as default plugin"): <https://index.ros.org/p/rosbag2_storage_default_plugins/>
- `ros2 bag record` y su caché (`--max-cache-size`, doble buffer, modo *snapshot*): <https://github.com/ros2/rosbag2/blob/dcd239ceea842752c344f0048572deb5f502feb8/ros2bag/ros2bag/verb/record.py>; cómo saber si se perdieron mensajes: <https://answers.ros.org/question/391514/detect-messages-dropped-by-ros2-bag-record/>
- La especificación de MCAP: <https://mcap.dev/spec> y su registro de codificaciones: <https://mcap.dev/spec/registry>. Los registros, los *opcodes*, la compresión y la nota "Custom profiles and encoding strings are allowed" se revisaron en el código de la biblioteca oficial de Python, `mcap` 1.6.0 (licencia MIT): <https://pypi.org/project/mcap/>
- Archivos de MCAP cortados (`mcap doctor`, `mcap recover`): <https://docs.foxglove.dev/docs/importing-data>
- Las codificaciones que Foxglove decodifica: <https://docs.foxglove.dev/docs/getting-started/custom/custom-schema-encodings>; sus extensiones convierten de un esquema a otro: <https://foxglove.dev/blog/announcing-message-converter-extensions-in-foxglove>
- dora-rs, `dora record` y `dora replay`: <https://docs.rs/crate/dora-cli/1.0.0-rc.5/source/src/command/record.rs>, <https://docs.rs/crate/dora-cli/1.0.0-rc.5/source/src/command/replay.rs>; el nodo `dora-record` (Parquet): <https://lib.rs/crates/dora-record>
- LeRobot, formato de datasets v3.0: <https://huggingface.co/docs/lerobot/lerobot-dataset-v3>
- Rerun, formato `.rrd`: <https://rerun.io/docs/concepts/logging-and-ingestion/rrd-format>
- Arrow IPC, formato de *stream* y de archivo: <https://arrow.apache.org/docs/r/reference/read_ipc_stream.html>
