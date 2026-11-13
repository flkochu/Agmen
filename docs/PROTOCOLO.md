# Protocolo de cable de Agmen — versión 1

> **Estado:** implementado en Agmen 0.4.x (nivel 1). Diseño completo y razones
> en el [RFC 0001](rfc/0001-formato-de-mensajes.md) y su
> [anexo A](rfc/0001-formato-de-mensajes.md#anexo-a--nivel-1-texto-canónico-y-anuncio-de-esquema).
> La versión 0 (Agmen 0.1–0.3, cabecera de 32 bytes) ya no se acepta: un
> nodo 0.4 que recibe un mensaje v0 lo rechaza diciendo que se actualice.

Este documento es **el contrato entre lenguajes**. Un nodo escrito en C++,
Python, JavaScript o en un microcontrolador es compatible si escribe y lee
exactamente estos bytes. Hay dos implementaciones de referencia, C++
(`include/agmen/wire.hpp` + `src/wire.cpp`) y Python (`python/agmen/wire.py` +
`python/agmen/schema.py`), y una tercera que solo lee, en JavaScript
(`python/agmen/panel/agmen.js`, la del panel web, desde 0.6.0). Las tres se
revisan contra los mismos **vectores de prueba**: [`tests/vectors/v1.txt`](../tests/vectors/v1.txt).

## Reglas generales

- Todos los enteros y flotantes van en **little-endian**.
- Los flotantes son IEEE-754 (`f32` = 4 bytes, `f64` = 8 bytes).
- Transporte de los `compact`: **un mensaje = un datagrama UDP**. Los `arrow`
  van por TCP (RFC 0008) y, en la misma computadora, por memoria compartida
  (RFC 0009, abajo). Desde Agmen 0.5 (nivel 2)
  los emisores averiguan a quién mandar con el `agmen hub`
  ([RFC 0004](rfc/0004-topics-y-descubrimiento.md), protocolo de control
  `agmen-hub/1`); el formato de los datos no cambió.
- Los hashes son **FNV-1a de 64 bits**: `h = 0xcbf29ce484222325`; por cada
  byte, `h ^= byte; h *= 0x100000001b3` (mod 2⁶⁴). `fnv1a64("") =
  0xcbf29ce484222325`, `fnv1a64("a") = 0xaf63dc4c8601ec8c`.

## Sobre (64 bytes)

| Offset | Tamaño | Tipo | Campo | Descripción |
|---:|---:|---|---|---|
| 0 | 3 | bytes | `magic` | Siempre `"AGM"` (0x41 0x47 0x4D) |
| 3 | 1 | u8 | `version` | `1` |
| 4 | 2 | u16 | `header_len` | Tamaño del sobre: `64`. El receptor salta lo que pase de 64 (sobres futuros) |
| 6 | 1 | u8 | `encoding` | `0` sin payload · `1` compact · `2` arrow (nivel 7) · `3` anuncio de esquema |
| 7 | 1 | u8 | `flags` | bit 0 `SHM`: el payload es un descriptor de memoria compartida (desde 0.11, abajo). bit 1 `FIN`. El resto en 0 |
| 8 | 8 | u64 | `schema_id` | FNV-1a 64 del texto canónico del esquema |
| 16 | 8 | u64 | `topic_id` | FNV-1a 64 del nombre del topic (`"/imu"`); 0 = sin topic |
| 24 | 8 | u64 | `source_id` | Número aleatorio ≠ 0 de la instancia que publica |
| 32 | 8 | u64 | `seq` | Contador por (`source_id`, `topic_id`), empieza en 0 |
| 40 | 8 | i64 | `t_data` | Instante al que corresponden los datos, ns desde la época Unix |
| 48 | 8 | i64 | `t_pub` | Instante en que se publicó, ns desde la época Unix |
| 56 | 4 | u32 | `payload_len` | Bytes que siguen al sobre |
| 60 | 1 | u8 | `clock` | `0` real · `1` simulación · `2` reproducción |
| 61 | 3 | — | reservado | En 0 |
| 64 | N | bytes | payload | Según `encoding` |

## Esquemas

```
agmen/1 Nombre{tipo campo;tipo campo;...}
```

| Tipo | Bytes (= alineación) |
|---|---:|
| `bool` `i8` `u8` | 1 |
| `i16` `u16` | 2 |
| `i32` `u32` `f32` | 4 |
| `i64` `u64` `f64` `time` `duration` | 8 |
| `T[N]`, `T[N][M]` | N × tamaño de T (alineación de T) |
| `Nombre{...}` anidado | según sus campos (alineación = la del campo más exigente) |

**Texto canónico** (lo que se hashea): prefijo `agmen/1 `, sin espacios
excepto **uno** entre tipo y nombre, `;` entre campos y sin `;` final, y los
structs anidados **expandidos** en el lugar donde se usan. Las herramientas
de Python aceptan espacios de más y los normalizan; en C++ `kSchema` debe
estar ya en forma canónica (sin el prefijo) y se valida al compilar.

**Límites** (los mismos en C++, Python y JavaScript; un esquema que no los
cumple se rechaza, también en un anuncio):

| | Máximo |
|---|---|
| `N` de `T[N]` | 1 048 576 |
| `N` de `tensor<T,N>` | 32 |
| Niveles anidados (cada struct y cada `[N]` o `[]` es uno: `Pose{Vec3{f64 x} p}` usa 2, `A{f32[2][3] m}` usa 3) | 32 |
| Tamaño de un tipo de tamaño fijo (y por tanto de un mensaje `compact`) | 2 GiB − 1 (2 147 483 647 bytes) |

El texto de un esquema llega de la red en los anuncios: estos límites hacen
que leerlo cueste lo mismo que su largo, sin importar qué mande el emisor.

## Payload `compact` (encoding = 1)

El layout de un `struct` de C:

1. Los campos van en el orden del esquema.
2. Cada campo empieza en un múltiplo de su alineación; los bytes de relleno
   van **en 0**.
3. El tamaño total se redondea al múltiplo de la alineación del struct.
4. Un payload puede traer N filas seguidas (`payload_len` = N × tamaño). Las
   herramientas genéricas aceptan N > 1; los nodos tipados esperan N = 1.

Ejemplo, `agmen/1 JointCommand7{u8 mode;f64[7] position}` (64 bytes):

```
byte 0       mode
bytes 1–7    relleno (0)
bytes 8–63   position[0..6], 8 bytes cada uno
```

## Anuncio de esquema (encoding = 3)

Cada emisor manda un anuncio **antes de su primer dato y luego una vez por
segundo**, al mismo destino que sus datos:

- `schema_id` = hash del texto canónico; `topic_id` = hash del topic;
  `source_id` y `seq` = los del emisor; `t_data = t_pub` = ahora.
- payload = UTF-8 de `"<topic>\n<texto canónico>"`, por ejemplo
  `"/imu\nagmen/1 ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}"`.

El receptor valida que el texto y el topic coincidan con sus hashes. Un
anuncio **nunca** se entrega como dato: los nodos tipados lo usan para dar
mejores errores, y las herramientas (`agmen echo`, el panel) para decodificar
cualquier tipo sin código compilado.

## Validación al recibir

El receptor rechaza el paquete y explica por qué si:

| # | Condición |
|---|---|
| 1 | Mide menos de 64 bytes o menos que `header_len` |
| 2 | `magic` ≠ `"AGM"`; `version` ≠ 1 (si es 0, el mensaje dice que el emisor es Agmen 0.1–0.3) |
| 3 | `encoding` > 3 o un bit de `flags` desconocido; `SHM` en un paquete que no llegó por una conexión que pidió memoria compartida |
| 4 | `payload_len` ≠ bytes restantes |
| 5 | `schema_id` ≠ el que espera el receptor (`TIPO INCOMPATIBLE`, con el esquema que llegó si ya se anunció) |
| 6 | `compact`: el payload no mide lo que dice el esquema |

## Memoria compartida (flags.SHM, desde Agmen 0.11)

Diseño y razones en el [RFC 0009](rfc/0009-memoria-compartida.md). Solo para
mensajes `arrow` de 4 KiB o más, entre un publicador y un suscriptor de la misma
computadora, por la conexión TCP de los mensajes grandes (RFC 0008):

1. Al aceptar la conexión, el suscriptor manda un **aviso** "puedo" (abajo). Un
   suscriptor que no lo manda nunca recibe `SHM`.
2. El publicador le manda una **prueba**: un sobre con `encoding = 0`,
   `flags = SHM` y el descriptor de un bloque de 4 KiB. El suscriptor lo abre,
   revisa la cabecera y contesta "sí pude" o "no pude". Una prueba nunca se
   entrega como mensaje.
3. Si pudo, cada mensaje grande viaja como sobre con `flags = SHM`,
   `encoding = 2` (lo que hay en el bloque) y `payload_len = 72`, seguido del
   descriptor. Si no, como siempre.
4. Cuando ya nadie usa el mensaje, el suscriptor manda "suelto el bloque".

**Descriptor** (72 bytes, el payload en línea):

| Offset | Tamaño | Tipo | Campo |
|---:|---:|---|---|
| 0 | 32 | ASCII | `name`: "/agm…", relleno con ceros (en Windows se abre como `Local\agm…`) |
| 32 | 8 | u64 | `size`: bytes del bloque |
| 40 | 8 | u64 | `offset`: dónde empieza el payload (64) |
| 48 | 8 | u64 | `length`: bytes del payload |
| 56 | 8 | u64 | `generation`: el `seq` del mensaje |
| 64 | 4 | u32 | `slot`: número de bloque del publicador |
| 68 | 4 | — | reservado, 0 |

**Cabecera de cada bloque** (64 bytes al principio del bloque): `"AGMSHM1\0"`
(8), `owner` = `source_id` del publicador (u64), `generation` (u64), `length`
(u64), `size` (u64) y 24 bytes en 0. El suscriptor revisa que `owner`,
`generation` y `length` coincidan con el sobre y el descriptor.

**Avisos del suscriptor** (16 bytes cada uno, por la misma conexión TCP, en
sentido contrario): `"AGB"`, `op` (u8: 1 = puedo, 2 = la prueba salió bien, 3 =
la prueba falló, 4 = suelto el bloque), `slot` (u32) y `generation` (u64).

## Grabaciones (MCAP, desde Agmen 0.12)

Diseño y razones en el [RFC 0010](rfc/0010-grabar-y-reproducir.md). Una
grabación de Agmen es un archivo [MCAP](https://mcap.dev/spec) con el perfil
`agmen` y la codificación de mensajes `agmen`. Cualquier biblioteca de MCAP lo
abre, lo recorta y lo junta; para entender los mensajes basta este documento.

| En MCAP | Qué guarda Agmen |
|---|---|
| `Header` | `profile = "agmen"`, `library = "agmen 0.12.0"` |
| `Schema` | `name` = el nombre del tipo (`Imagen`), `encoding = "agmen"`, `data` = el esquema canónico en UTF-8 (`agmen/1 Imagen{…}`), el mismo texto que viaja en el anuncio |
| `Channel` | uno por topic: `topic = "/camara"`, `message_encoding = "agmen"`; en sus metadatos, `schema_id` (16 cifras hexadecimales) y `encoding` (`compact` o `arrow`) |
| `Message` | `data` = **el paquete de Agmen completo** (sobre de 64 bytes + payload, como arriba); `publish_time` = `t_pub`; `log_time` = cuándo lo recibió el grabador (en el reloj del robot: real, sim o replay); `sequence` = los 32 bits bajos de `seq` |
| `Metadata` | `agmen.grabacion`: `version`, `computadora`, `reloj` (real, sim, replay), `inicio`, `topics` y, con `agmen run`, `robot`, `comando` y `perfil` (opcional, desde 0.14.0: el perfil con que corrió `agmen run`, ej. `real`, `sim`, `replay`; `agmen record` suelto no lo escribe, y las grabaciones sin este campo se leen igual). `agmen.perdidos`: `"<topic> <motivo>"` → cuántos se perdieron (`disco`, `antes de llegar` = huecos de `seq`, `sin anuncio`) y `total` |
| `Attachment` | con `agmen run`, el `robot.toml` (`text/plain`) |
| *Chunks* | de unos 4 MiB o 1 s, con `MessageIndex`; sin comprimir o con `zstd`/`lz4`; resumen completo (`ChunkIndex`, `Statistics`, `MetadataIndex`…) al cerrar |

- Lo que llegó por memoria compartida se guarda con su payload de verdad y sin
  la bandera `SHM`: queda igual que si hubiera llegado por TCP.
- Un `Message` no lleva el `/clock` que manda `agmen play`: al reproducir, el
  tiempo lo publica el reproductor.
- Al reproducir, `agmen play` manda los mismos bytes con el campo `clock` del
  sobre = 2 (`replay`).
- Hay tres lectores de referencia: Python (`python/agmen/_mcap.py`),
  JavaScript (`python/agmen/panel/mcap.js`, el del panel) y la biblioteca
  oficial de MCAP. Los tres leen igual el vector
  [`tests/vectors/grabacion.mcap`](../tests/vectors/grabacion.mcap).

## Topics de Agmen

Algunos topics los publica Agmen mismo. No hace falta declararlos en
`robot.toml`, y el hub avisa si alguien los usa con otro tipo.

| Topic | Tipo | Quién lo publica |
|---|---|---|
| `/clock` | `Clock{time t;f64 speed;bool paused}` | `agmen run` (`[clock] source = "agmen"`), tu simulador o `agmen play`: el tiempo de la simulación o de la grabación ([RFC 0006](rfc/0006-executor-y-reloj.md)) |
| `/mux/<nombre>` (desde 0.13) | `agmen.msgs.MuxEstado{string mux;string entrada;string topic;i32 prioridad;time desde;string antes;bool callada;u64 fuente;u32 cambios}` (`arrow`) | Cada `[[mux]]`: quién manda, cada vez que cambia y una vez por segundo ([RFC 0012](rfc/0012-mandos-del-panel-y-quien-manda.md)). `entrada = ""` si nadie manda; `callada = true` si el cambio fue porque `antes` dejó de hablar |

El nombre de `/mux/<nombre>` sale del nombre del `[[mux]]`, con las letras
que no valen en un topic cambiadas por `_`.

## Ejemplo completo

`MotorCommand{left = 0.5, right = -0.25}`, topic `/cmd`, `source_id =
0x0123456789abcdef`, `seq = 7`, `t_data = 1700000000000000000`, `t_pub =
t_data + 250 µs`:

```
41 47 4d 01 40 00 01 00                          AGM, v1, header_len 64, compact, flags 0
dd f5 91 84 70 fa 89 7b                          schema_id 0x7b89fa708491f5dd
0e b9 e7 a9 9c 04 1b 25                          topic_id  fnv1a64("/cmd")
ef cd ab 89 67 45 23 01                          source_id
07 00 00 00 00 00 00 00                          seq 7
00 00 2a 36 fe 9c 97 17                          t_data
90 d0 2d 36 fe 9c 97 17                          t_pub
08 00 00 00 00 00 00 00                          payload_len 8, clock 0, reservado
00 00 00 3f 00 00 80 be                          left 0.5, right -0.25
```

Los bytes exactos de este y otros ejemplos están en los vectores de prueba.
