# RFC 0001 — Formato de mensajes de Motio (v1)

| | |
|---|---|
| **Estado** | Propuesta, pendiente de aprobación |
| **Reemplaza a** | Protocolo v0 (`docs/PROTOCOLO.md`) |
| **Fecha** | 2026-10-30 |
| **Nota** | El núcleo pasó de Rust a C++ el mismo día. El formato no depende del lenguaje, así que esta propuesta no cambia |

## 1. Resumen

Cada mensaje de Motio tiene dos partes:

1. **Sobre (envelope):** 64 bytes con formato fijo, propio de Motio. Lleva
   identidad, orden y tiempo: qué tipo es, de qué topic viene, quién lo
   publicó, número de secuencia y dos marcas de tiempo.
2. **Carga (payload):** los datos, en una de dos codificaciones elegidas
   **automáticamente según el esquema**:
   - **`compact`**: para mensajes de tamaño fijo (IMU, comandos de motor,
     estados de articulaciones). Es el layout de un struct de C. Es diminuto,
     se lee sin copiar desde C++, numpy y cualquier lenguaje con structs, y cabe en un microcontrolador.
   - **`arrow`**: para datos de tamaño variable o grandes (imágenes,
     tensores, nubes de puntos, texto, listas). Es un RecordBatch de Apache
     Arrow. Se lee sin copiar desde pyarrow, numpy y PyTorch, y se convierte
     directo a Parquet para datasets.

Las dos codificaciones describen **el mismo modelo lógico**: todo esquema de
Motio se mapea a un esquema de Arrow. Así, cualquier mensaje, sea compacto o
Arrow, se puede ver, grabar y exportar como una fila de una tabla Arrow.

## 2. Por qué no "Arrow para todo"

Arrow es el formato correcto para datos grandes, pero no para lazos de control
rápidos. Medido en este repo (`bench/arrow_vs_compact.py`, Linux, Python
3.13, pyarrow 26):

| Mensaje | `compact` | Arrow IPC (sin esquema) |
|---|---:|---:|
| `MotorCommand` (2 × f32), tamaño | 8 B | 208 B |
| `ImuSample` (6 × f32), tamaño | 24 B | 432 B |
| `ImuSample` en Python, codificar | 0.16 µs | 32 µs |
| `ImuSample` en Python, decodificar | 0.29 µs (numpy, sin copia) | 3.2 µs |
| Imagen 640×480×3 u8, sobrecosto | — | 176 B (0.02 %), numpy sin copia ✔ |

- Para un comando de motor a 1 kHz, Arrow multiplica el tamaño por 26 y, en
  Python, el costo de codificar por 200.
- Un microcontrolador no puede correr una librería de Arrow, pero un struct de
  C sí.
- Para una imagen, el sobrecosto de Arrow es despreciable y lo que ganas es
  enorme: lectura sin copias y compatibilidad con todo el ecosistema de datos.

Conclusión: **dos codificaciones con un solo modelo lógico**.

## 3. El sobre (envelope) — 64 bytes

Todo en **little-endian**. El payload empieza en el byte 64, así que queda
alineado a 64 bytes (lo que Arrow recomienda y lo que necesitan SIMD y la GPU).

| Offset | Tamaño | Tipo | Campo | Descripción |
|---:|---:|---|---|---|
| 0 | 3 | bytes | `magic` | Siempre `"MOT"` |
| 3 | 1 | u8 | `version` | `1` |
| 4 | 2 | u16 | `header_len` | Tamaño del sobre. `64` en v1. El receptor salta los bytes extra que no conozca: así se pueden agregar campos sin romper nodos viejos |
| 6 | 1 | u8 | `encoding` | `0` = sin payload, `1` = `compact`, `2` = `arrow`. Del 3 al 255 están reservados |
| 7 | 1 | u8 | `flags` | bit 0 `PAYLOAD_EN_SHM`: el payload es un descriptor de memoria compartida (sección 7). bit 1 `FIN`: último mensaje del stream o episodio. El resto debe ir en 0 |
| 8 | 8 | u64 | `schema_id` | Hash del esquema canónico (sección 5) |
| 16 | 8 | u64 | `topic_id` | FNV-1a 64 del nombre del topic, ej. `"/imu"` |
| 24 | 8 | u64 | `source_id` | Identificador aleatorio de la instancia que publica; cambia si el nodo se reinicia |
| 32 | 8 | u64 | `seq` | Contador por (`source_id`, `topic_id`); empieza en 0 |
| 40 | 8 | i64 | `t_data` | Instante al que **corresponden los datos** (captura del sensor, instante del comando), en ns |
| 48 | 8 | i64 | `t_pub` | Instante en que **se publicó**, en ns. Lo pone Motio |
| 56 | 4 | u32 | `payload_len` | Bytes del payload que siguen al sobre |
| 60 | 1 | u8 | `clock` | Dominio del reloj: `0` = real (Unix, UTC), `1` = simulación, `2` = reproducción |
| 61 | 3 | — | reservado | Debe ir en 0 |

### Decisiones del sobre

- **`header_len` desde el día uno.** Es la lección más cara de los formatos
  de red: si no dejas cómo crecer, cualquier campo nuevo rompe a todos.
- **Dos marcas de tiempo.** `t_data` es lo que importa para fusión de
  sensores y datasets ("la cámara vio esto en t"). `t_pub` sirve para medir
  latencia y depurar. Un nodo de IA a 10 Hz que usa una imagen de hace 80 ms
  necesita distinguirlas.
- **Tiempos `i64` en ns**: se mapean directo al tipo `timestamp[ns]` de Arrow
  y Parquet.
- **`clock` en cada mensaje.** Los nodos nunca leen el reloj del sistema
  operativo, sino el reloj de Motio. En simulación lo maneja el simulador y
  en reproducción la grabación. El nodo no se entera de la diferencia
  (requisito d), y si alguien mezcla datos de dominios distintos, se detecta.
- **`source_id` + `seq`**: detectan pérdidas, duplicados, reinicios y varios
  publicadores en el mismo topic.
- **Lo que no va en el sobre**: QoS, `frame_id` y unidades. Son metadatos
  **del topic** y se declaran una sola vez al anunciarlo, en vez de mandar un
  string en cada mensaje como hace ROS 2.

## 4. Codificación `compact` (encoding = 1)

### Cuándo aplica

Cuando **todos** los campos del esquema son de tamaño fijo:

- `bool` (1 byte, 0 o 1), `i8`, `u8`, `i16`, `u16`, `i32`, `u32`, `i64`, `u64`,
  `f32`, `f64`
- `time` (i64 ns) y `duration` (i64 ns)
- arreglos fijos `T[N]` y `T[N][M]`
- structs anidados que cumplan estas mismas reglas

Si hay un solo campo variable (`string`, `T[]`, tensor de forma dinámica), el
esquema usa `arrow`.

### Layout = struct de C

1. Los campos van en el orden del esquema.
2. Cada campo se alinea a su tamaño natural: `f64` a 8, `f32` a 4, `u8` a 1.
   Un arreglo se alinea como su elemento y un struct como su campo más
   exigente.
3. El tamaño del struct se redondea al múltiplo de su alineación.
4. Los bytes de relleno **deben ir en 0**, para que las grabaciones y los
   hashes sean deterministas.
5. Un payload puede llevar **N filas**: N structs contiguos, con
   `N = payload_len / tamaño_del_struct`. Normalmente N = 1.

Esto coincide con un `struct` de C/C++ en x86-64,
ARM64 y ARM32, con `numpy.dtype(..., align=True)` y con `#[repr(C)]` en Rust. El código generado (nivel
4) incluye `static_assert` de offsets para detectar ABIs raros.

### Ejemplos

```
MotorCommand { f32 left; f32 right; }        → 8 bytes
  0 left | 4 right

ImuSample { f32 ax; f32 ay; f32 az;          → 24 bytes
            f32 gx; f32 gy; f32 gz; }
  (idéntico al payload v0: el código de Nivel 0 sigue sirviendo)

JointCommand7 { u8 mode; f64 position[7]; }  → 64 bytes
  0 mode | 1..7 relleno (0) | 8 position[0..7]
```

### Lectura sin copias

| Lenguaje | Cómo |
|---|---|
| C++ | struct generado con `static_assert` de offsets; se lee con `std::memcpy`/`std::bit_cast` (o vista directa si el buffer está alineado) |
| Python | `np.frombuffer(payload, dtype)`; 0.29 µs medido |
| Arrow | con 1 fila, cada campo es un buffer de Arrow apuntando a su offset; con N filas, convertir a columnas es una copia barata (los mensajes son chicos por definición) |

## 5. Esquemas y `schema_id`

- Cada topic tiene **exactamente un esquema**. No hay topics sin tipo.
- El esquema se escribe en el lenguaje de esquemas de Motio, que se define en
  el nivel 4. Hasta entonces, la forma canónica es texto:
  `motio/1 Nombre{tipo campo;tipo campo;...}`. Los tipos anidados se
  **expanden dentro** del texto, para que el hash cubra todo el árbol de
  tipos.
- `schema_id = FNV-1a 64(texto canónico)`. Con 10 000 tipos distintos, la
  probabilidad de que dos colisionen es de ~3×10⁻¹².
- **El texto completo del esquema se distribuye una vez**: al anunciar el
  topic y al inicio de cada grabación. Por eso cualquier herramienta
  (`motio topic echo`, un visualizador, un exportador a Parquet) puede
  decodificar cualquier mensaje **sin código compilado** para ese tipo. En
  ROS 2, para hacer `echo` necesitas el paquete del mensaje instalado.
- **Compatibilidad estricta**: si cambia cualquier campo, cambia el
  `schema_id`, y el suscriptor rechaza los mensajes con un error claro. Las
  versiones nuevas se nombran explícitamente (`ImuSample2`). Es más simple y
  nunca falla en silencio.

### Mapeo canónico a Arrow

| Motio | Arrow |
|---|---|
| `bool`, `iN`, `uN`, `f32`, `f64` | tipo primitivo equivalente |
| `time` | `timestamp[ns]` |
| `duration` | `duration[ns]` |
| `T[N]` | `FixedSizeList<T>[N]` |
| `T[N][M]` (arreglo fijo multidimensional) | `arrow.fixed_shape_tensor` |
| `T[]` | `List<T>` |
| `string` | `Utf8` |
| `bytes` | `Binary` |
| `tensor<T>` (forma dinámica) | `arrow.variable_shape_tensor` |
| struct anidado | `Struct` |
| `optional T` | campo nullable (los demás son no-nullables) |

Los tensores usan las **extensiones canónicas de Arrow**
(`arrow.fixed_shape_tensor`, `arrow.variable_shape_tensor`). Así PyTorch,
numpy y otras herramientas las entienden sin código especial de Motio.

## 6. Codificación `arrow` (encoding = 2)

- El payload es un **mensaje encapsulado de Arrow IPC con un RecordBatch**:
  el marcador de continuación, la longitud de los metadatos, los metadatos
  (flatbuffer) y el cuerpo.
- **El esquema de Arrow no viaja en cada mensaje.** Se deriva del esquema de
  Motio (sección 5), que el receptor ya conoce por su `schema_id`.
- Un RecordBatch puede tener **N filas**. Arrow es columnar, así que agrupar
  (por ejemplo, 10 lecturas de un LiDAR por mensaje) es natural.
- **Python**: Motio entrega los datos por la *Arrow PyCapsule Interface*, el
  protocolo estándar para pasar datos Arrow entre librerías sin copiar. Así
  funcionan pyarrow, Polars, DuckDB y nanoarrow sin que Motio dependa de
  ninguna. Los tensores pasan a numpy sin copia (verificado) y de ahí a
  PyTorch con `torch.from_numpy`.
- **C++**: por la *Arrow C Data Interface*, que es compatible con Arrow C++ o
  con nanoarrow (más ligero).

## 7. Memoria compartida (solo se reserva espacio en v1)

Si `flags.PAYLOAD_EN_SHM = 1`, el payload en línea no son los datos sino un
**descriptor** del bloque de memoria compartida donde viven (segmento, offset,
longitud y generación). Los datos dentro del bloque siguen las mismas reglas
de `compact` o `arrow`, alineados a 64 bytes.

La semántica exacta (quién reserva, cómo se libera, préstamo de buffers para
que una cámara escriba directo en memoria compartida) se define en el nivel 8.
**Este RFC solo garantiza que el formato no lo impide.**

## 8. Validación obligatoria al recibir

El receptor rechaza el mensaje **y reporta el motivo** si:

1. tiene menos de 64 bytes, o menos de `header_len`;
2. `magic` ≠ `"MOT"` o `version` no es soportada;
3. `encoding` o algún bit de `flags` es desconocido;
4. `payload_len` ≠ bytes restantes;
5. `schema_id` ≠ el que espera el suscriptor;
6. en `compact`, `payload_len` no es múltiplo del tamaño del struct;
7. en `arrow`, el RecordBatch no coincide con el esquema;
8. `clock` no coincide con el dominio de la sesión. Esto se reporta como
   advertencia, para detectar mezclas de datos simulados y reales.

## 9. Cómo cubre cada requisito

| Requisito | Cómo lo cubre el formato |
|---|---|
| a) Datos grandes sin copias | `arrow` + tensores canónicos + bandera de memoria compartida; payload alineado a 64 |
| b) Python de primera clase, C++ también | `compact` = dtype de numpy; `arrow` = PyCapsule. En C++, structs y Arrow C Data Interface |
| c) Todo con marca de tiempo; grabaciones = datasets | `t_data`/`t_pub` obligatorios; esquemas autodescritos; mapeo canónico a Arrow, así que exportar a Parquet es directo |
| d) Misma interfaz sim/real | campo `clock` + reloj de Motio; el nodo nunca lee el reloj del sistema operativo |
| e) Frecuencias distintas sin bloquearse | `compact` mantiene barato el control a 1 kHz; el no bloqueo se resuelve en el executor (nivel 5), no en el formato |
| f) IoT por puentes; versión para microcontroladores | esquemas en texto, así que un puente MQTT convierte a JSON automáticamente. `compact` se implementa en C sin dependencias |
| g) Núcleo nativo (C++), Windows/Linux/macOS | formato sin nada específico del sistema operativo, little-endian, verificado por CI |

## 10. Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Arrow para todo (como dora-rs) | 18–26× más bytes y 200× más lento en Python para mensajes de control; imposible en un microcontrolador |
| FlatBuffers / Cap'n Proto | Lectura sin copias para mensajes chicos, pero sin ecosistema de datasets y tensores; los esquemas se compilan sí o sí |
| Protobuf / CDR (lo que usa ROS 2) | Requieren deserializar (copiar); no sirven para imágenes y tensores sin copia |
| Solo structs de C | No pueden representar tamaño variable |

## 11. Versión ligera para microcontroladores (boceto, se define en el nivel 12)

El enlace serial o UDP con un microcontrolador usa un **sobre corto** (~12
bytes: id de topic corto, seq de 16 bits, `t_data` en µs de 32 bits,
longitud y CRC). El **agente** del lado de la computadora lo traduce al sobre
completo. El payload `compact` es **idéntico** en ambos lados, así que el
microcontrolador nunca necesita Arrow ni un sobre de 64 bytes.

## 12. Plan de implementación por niveles

El formato completo **no** se implementa de golpe. El sobre v1 ya tiene
espacio para todo, así que cada nivel agrega sin romper:

| Nivel | Qué parte del formato |
|---|---|
| 1 | Sobre v1 completo + `compact` + esquema en texto + decodificación dinámica (`motio topic echo` genérico). Sigue sobre UDP |
| 3 | El navegador decodifica el formato (implementación en JavaScript, RFC 0002) |
| 4 | Lenguaje de esquemas + generación de código (C++/Python) para `compact` |
| 7 | Codificación `arrow`: variables, imágenes, tensores |
| 8 | Memoria compartida (`PAYLOAD_EN_SHM`) |
| 9 | Archivo de grabación + exportar a Parquet |
| 12 | Sobre ligero para microcontroladores |

## 13. Preguntas abiertas

1. **Formato del archivo de grabación**: ¿MCAP (formato abierto con
   herramientas como Foxglove) con mensajes de Motio adentro, o un formato
   propio basado en Arrow IPC? Se decide en el nivel 9. El sobre funciona con
   ambos.
2. **Hash de 64 bits vs 128 bits** para `schema_id`. 64 es suficiente para
   uso personal; si Motio crece a un ecosistema público, se puede pasar a 128
   usando `header_len`.
3. **Sincronización de relojes entre máquinas** (NTP/PTP, o un reloj lógico
   híbrido como el que usa dora-rs). Se decide en el nivel 11. No cambia el
   formato.
