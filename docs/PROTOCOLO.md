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

## Topics y hub: `agmen-hub/1`

> **Estado:** el modo local (lo de siempre) está implementado desde Agmen 0.5.0
> ([RFC 0004](rfc/0004-topics-y-descubrimiento.md), §4.2). Lo marcado
> **(desde 0.14.0)** es el soporte de los topics entre computadoras
> ([RFC 0011](rfc/0011-sim-real-y-varias-computadoras.md), §3.5, partes 4a y 4b): el
> hub y los dos núcleos (C++ y Python) ya lo entienden, y `agmen run` y `agmen agent`
> encienden el modo red y pasan las tablas de sus hubs por la conexión del agente
> (ver [El puente de topics](#el-puente-de-topics-tablas-desde-0140)). La prueba de red
> (parte 4c) está en [La prueba de red](#la-prueba-de-red-sonda-desde-0140). Lo que se ve
> en herramientas y panel no cambia el protocolo: usa `machine=`, que ya existe en las
> entradas de otra computadora.

Es el protocolo de control entre los nodos y su **hub** (`agmen hub`, o el que
arranca `agmen run`). Los datos no pasan por aquí: el hub solo dice a quién
mandarle cada topic. Referencia de código: `python/agmen/hubproto.py` (el
docstring tiene la lista completa) y `include/agmen/hubproto.hpp`.

### Forma de la línea

```
agmen-hub/1 <operación> clave=valor clave=valor ...
```

- Un datagrama UDP trae una línea de texto UTF-8. La respuesta `state` trae varias.
- Los valores escriben `%XX` por cada byte que no sea letra, dígito o `_ . / : , * -`.
  Por eso `@` sale como `%40` y el espacio como `%20`. Al leer se aceptan las dos
  formas: `robot@192.168.1.20` y `robot%40192.168.1.20` son lo mismo.
- Los datos (`AGM…`) y el control (`agmen-hub/1…`) llegan al mismo socket. Se
  distinguen por los primeros bytes.
- El hub escucha en `127.0.0.1:7390` (o en `AGMEN_HUB`).
- Cada endpoint late cada 1 s (`BEAT_S`). Sin latir 3 s (`EXPIRE_S`), el hub lo
  da por muerto.

### Operaciones de siempre (desde 0.5.0)

| De → a | Operación | Campos | Para qué |
|---|---|---|---|
| nodo → hub | `pub` | `id node pid topic schema type text` | Registrar o latir un Publisher |
| nodo → hub | `sub` | `id node pid topic schema type port [tcp]` | Registrar o latir un Subscriber. `topic=*` = todos; `schema=*` = cualquier tipo. `tcp` = puerto TCP para mensajes grandes (RFC 0008) |
| nodo → hub | `bye` | `id` | El endpoint se va. Si no llega, expira a los 3 s |
| nodo → hub | `stat` | `node pid kind name …` | Cuentas de un `agmen.Node` (RFC 0006), cada segundo |
| herramienta → hub | `query` | — | Pide el estado completo: líneas `state`, `topic`, `ep` y `stat` |
| herramienta → hub | `ping` | — | ¿Hay hub? Contesta `pong` |
| hub → Publisher | `subs` | `id topic to=ip:puerto,… [tcp=…]` | A quién mandarle los datos. `to` son los puertos UDP y `tcp` los TCP. Se repite en cada latido |
| hub → Subscriber | `ok` | `id` | Latido recibido |
| hub → nodo | `error` | `id msg` | Registro rechazado (nombre inválido, un topic con dos tipos) |
| hub → herramienta | `pong` | `version` | Respuesta a `ping` |

Ejemplos (generados con `hubproto.encode`):

```
agmen-hub/1 pub id=2f9c1e0a7b3d4a11 node=cam pid=7 topic=/imu schema=00000000000000aa type=Imu text=Imu%7Bf32%20a%7D
agmen-hub/1 sub id=a1 node=imu pid=4321 topic=/imu schema=00000000000000aa type=Imu port=6000 tcp=6001
agmen-hub/1 subs id=2f9c1e0a7b3d4a11 topic=/imu to=127.0.0.1:6000,127.0.0.1:6010 tcp=6001
agmen-hub/1 ok id=a1
agmen-hub/1 error id=a1 msg=%27/imu%27%20ya%20es%20de%20tipo%20Imu
```

### Modo red (desde 0.14.0)

El hub arranca en **modo local**: como hoy, todo se queda en esta computadora.
Pasa a **modo red** cuando recibe de su propia computadora la operación `red`. Las
otras computadoras van en `peers`, con su nombre de `[[machine]]` y la IP con la
que **esta** computadora las ve:

```
agmen-hub/1 red peers=robot%40192.168.1.20,laptop%40192.168.1.5
agmen-hub/1 red_ok peers=2
agmen-hub/1 red peers=
agmen-hub/1 red_ok peers=0
```

- `red_ok peers=<n>`: cuántas computadoras aceptó. `error msg=…` si un nombre o una
  IP no sirven, si se repiten, o si la IP es `127.0.0.1` (es esta computadora).
- Un nombre es como `[[machine]] name` (letras, dígitos, `_` o `-`). Una IP son
  cuatro números de 0 a 255, sin ser `0.0.0.0`, una de grupo (224 a 239) ni
  `255.255.255.255`.
- `peers=` vacío vuelve a modo local.
- El modo red **caduca**. Quien lo encendió lo repite cada 2 s (`NET_BEAT_S`). Si
  en 6 s (`NET_EXPIRE_S`) no llega otro `red`, el hub vuelve a modo local y lo
  escribe en su log. Así, si muere quien lo encendió, nada queda abierto a la red.
- En modo red, `pong` lleva `red=1` y cada `ok` lleva `peers` (las IP de las que
  puede aceptar datos; sin nombres). En modo local, esos campos no van:

```
agmen-hub/1 ping
agmen-hub/1 pong version=0.13.0 red=1
agmen-hub/1 ok id=s1 peers=192.168.1.20,192.168.1.5
```

(`version` es la de tu Agmen. Hoy el código dice 0.13.0.)

- Al volver a modo local, el hub quita las entradas de otra computadora y avisa a
  los Publisher con `subs`, como cuando un Subscriber se va.

**Quién puede mandar estas operaciones.** `red`, `machine`, `addr` y `net` solo se
aceptan desde `127.0.0.1` (o desde `AGMEN_NET_IP`, ver abajo). Si llegan de otra IP,
el hub contesta `error`: otra computadora no puede abrir el hub a la red. Las tablas
de las otras computadoras llegan por `agmen agent` (ver
[`agmen-agent/1`](#conexión-con-clave-agmen-agent1-desde-agmen-0140)).

#### Entradas de otra computadora (desde 0.14.0)

En modo red, un proceso de esta computadora (en la 4b, el puente) puede registrar
endpoints **en nombre de otra computadora**:

```
agmen-hub/1 sub id=r1 node=cam pid=7 topic=/cmd schema=00000000000000aa type=Cmd machine=robot addr=192.168.1.20:7600 tcp=7700
agmen-hub/1 pub id=r2 node=mando pid=8 topic=/cmd schema=00000000000000aa type=Cmd text=Cmd%7Bf32%20v%7D machine=laptop
```

- `sub … machine=<nombre> addr=<ip>:<puerto> [tcp=<puerto>]`: un Subscriber que vive
  en `<nombre>`. Los datos van a `addr`.
- `pub … machine=<nombre>`: un Publisher de `<nombre>`. No lleva dirección, y el hub
  **no** le manda `subs`: eso lo hace el hub de su computadora. El hub contesta
  `ok` al proceso que lo registró.
- `solo=<id>,<id>…` o `solo=-` (desde 0.14.0): solo en un `sub` de otra computadora
  (con `machine`). Sin `solo`, el Subscriber de fuera recibe de **todos** los Publisher
  locales de su topic, como siempre. Con `solo`, solo de los Publisher cuyo `id` está
  en la lista; `solo=-` no le da ninguno. Lo usa el puente en una sesión de simulación
  o reproducción (ver [El puente de topics](#el-puente-de-topics-tablas-desde-0140)).
  Sin `machine` es un error.

Reglas (si no se cumplen, `error` con el porqué):

- Solo en modo red. Si el hub está en modo local, el error dice que primero hay que
  encender el modo red con `peers`.
- `machine` debe ser un nombre de `peers`. La IP de `addr` debe ser la de ese nombre.
- `net=1` no va en un endpoint de otra computadora.
- Caducan como cualquier endpoint: 3 s sin latir.
- Cuentan para todo lo demás: "un topic, un tipo" (también entre computadoras; el
  error dice de qué computadora es cada uno) y los `[[topic]]` esperados y
  obligatorios.
- Los `subs` a los Publisher de esta computadora incluyen a los Subscriber de fuera,
  con su `ip:puerto` y su `tcp`.

#### Subscriber local que escucha en la red (desde 0.14.0)

```
agmen-hub/1 sub id=a1 node=imu pid=4321 topic=/imu schema=00000000000000aa type=Imu port=6000 net=1
```

`net=1` quiere decir "mi puerto escucha en la red". Un Subscriber local **sin**
`net=1` mientras el hub está en modo red no es un error: su núcleo todavía no
cambió. Pero si pasan 5 s (`NET_LOCAL_WARN_S`) sin `net=1`, el hub lo avisa una vez
en su log, porque ese nodo no va a recibir de las otras computadoras. Casi siempre
es un Agmen anterior.

#### `state` con entradas de otra computadora (desde 0.14.0)

`query` contesta lo de siempre. Cada endpoint de otra computadora lleva `machine`, y
cada Subscriber local que escucha en la red lleva `net=1`:

```
agmen-hub/1 ep id=r1 kind=sub node=cam pid=7 topic=/cmd type=Cmd addr=192.168.1.20:7600 tcp=7700 age=0.4 machine=robot
```

#### `state` en varios datagramas (desde 0.14.0)

Si la respuesta a `query` cabe en un datagrama (hasta 65 507 bytes), sale **igual que
siempre, byte a byte**. Si no cabe, el hub la reparte en varios datagramas de como
mucho 32 KiB cada uno. Cada datagrama empieza con una línea `parte` y sigue con sus
líneas enteras, sin partir ninguna:

```
agmen-hub/1 parte i=1 de=2 n=7
agmen-hub/1 state version=0.14.0 hub=127.0.0.1:7390 robot=…
agmen-hub/1 topic name=/imu schema=00000000000000aa type=Imu …
agmen-hub/1 parte i=2 de=2 n=7
agmen-hub/1 ep id=a1 kind=sub node=imu pid=4321 topic=/imu …
```

- `i` va desde 1 y `de` es el total de partes. `n` distingue una respuesta de otra
  (el hub cuenta sus respuestas en partes).
- Una línea sola que no cabe en 32 KiB (un esquema enorme) va recortada y con
  `cortado=1`. Nunca se omite en silencio.
- Quien pregunta junta las partes con el mismo `n` hasta tener las `de`. Si al vencer
  el tiempo faltan partes, usa lo que llegó y lo dice: `la lista del hub llegó
  incompleta (k de m partes)`.
- **Un cliente anterior** lee solo el primer datagrama: ve una lista **parcial**. Antes
  de la 4b no veía nada, así que es una mejora, no un cambio de formato.

### Qué hacen los nodos (desde 0.14.0)

Los nodos cambian solo cuando su hub está en modo red. Con el modo local, sus bytes y
sus puertos (`127.0.0.1`) son los de siempre.

**Subscriber** (un Subscriber por topic; uno por dirección no cambia):

1. Cuando llega un `ok` con `peers` y el Subscriber escucha por topic, abre sockets
   nuevos en **todas las interfaces** (o en `AGMEN_NET_IP`): UDP y el escucha TCP.
   Se registra con el puerto nuevo, `tcp=` nuevo y `net=1`.
2. Sigue leyendo los sockets viejos hasta 1 s después de que el hub confirme el
   registro nuevo. Luego los cierra. Las conexiones TCP ya aceptadas no se cortan.
   Casi ningún mensaje se pierde; como mucho, los UDP que iban en vuelo.
3. Si no puede abrir los sockets nuevos, sigue como estaba, avisa una vez y lo
   reintenta con cada `ok`.
4. **De quién acepta datos** en modo red: de `127.0.0.1`, de `AGMEN_NET_IP` (si
   existe) y de las IP de `peers` del último `ok`. Un datagrama o una conexión TCP de
   otra IP se descarta, se cuenta en `rejected` y se avisa una vez por IP. Lo que
   viene de una computadora declarada, pero con el hub callado, también se descarta,
   con otro texto que dice la verdad.
5. Si un `ok` llega sin `peers` (el hub volvió a modo local), sigue con sus sockets y
   acepta solo lo local.
6. **Si el hub calla 6 s** (`NET_EXPIRE_S`) en modo red, vuelve a aceptar solo lo
   local, y lo dice una vez. Cuando el hub vuelve, vuelve a aceptar a los `peers`.
7. **El control solo vale si viene del hub.** Un mensaje `agmen-hub/1` que no sale de
   la dirección del hub (IP y puerto) se descarta sin efecto: no abre puertos, no
   cambia destinos ni la lista de IP. Se cuenta en `rejected` y se avisa una vez por
   remitente.
8. La caducidad se revisa **antes** de entregar lo que ya estaba leído de TCP. Pasado
   el plazo, no se entrega ni un mensaje más de una computadora de fuera.

**Publisher** (y el `RawPublisher` del `[[mux]]`):

1. Los destinos `127.0.0.1` (y `AGMEN_NET_IP`) van como siempre, por el socket de
   esta computadora. La **memoria compartida** solo se ofrece a estos destinos.
2. Un destino de otra IP en `subs` sale por un **socket de red aparte**, que se abre
   la primera vez que hace falta. Los mensajes chicos van por UDP; los grandes, por TCP
   (RFC 0008). El anuncio de esquema va por el mismo camino.
3. Si un destino de otra computadora no se alcanza, no frena a los demás: se cuenta
   en `unreachable` y se avisa una vez por destino, con la IP. Un envío que falla
   porque el búfer está lleno no cuenta como "inalcanzable".
4. Cuando ya no tiene destinos de otra computadora, cierra su socket de red. No queda
   un puerto abierto a la red.
5. Conectar por TCP a otra computadora espera hasta 3 s (`REMOTE_CONNECT_S`) sin
   frenar a los demás. A `127.0.0.1` son 1 s, como siempre.
6. Un receptor lento de otra computadora no frena a nadie y se cuenta.

### Topes de `query` (desde 0.14.0)

- **Una `query` por datagrama.** Si un mismo datagrama trae varias, el hub atiende la
  primera y las demás las ignora. Quien pregunta ya recibe una respuesta.
- **Partes pendientes de salir**: 16 MiB en total y 4 MiB por dirección. Si la respuesta
  nueva no cabe en esos topes, el hub no la arma ni la manda: no contesta a esa
  `query` (quien pregunta se queda sin respuesta y lo dice, y el hub lo escribe en su
  log). Es para que un cliente que no lee no deje sin lista a los demás.
- Una respuesta que cabe en un datagrama sale directamente, sin pasar por esa cola.

### Compatibilidad (desde 0.14.0)

- Los campos y las operaciones nuevos solo se **añaden**. Un nodo o un hub anterior
  que no los conozca sigue funcionando en modo local.
- Un nodo nuevo con un hub anterior: el hub nunca manda `peers`, así que el nodo
  queda en local.
- La **memoria compartida** sigue siendo de cada computadora. Entre computadoras, un
  mensaje grande va por TCP, como cualquier mensaje a otra computadora.
- **Una herramienta anterior** que lee `state` lee solo el primer datagrama de una
  respuesta en partes: ve una lista parcial, y no un error. Un hub anterior no reparte
  la lista: si no cabe en un datagrama, no contesta nada (como antes de la 4b).
- Sin `solo`, un `sub` de otra computadora recibe de todos los Publisher locales de su
  topic, como antes de la 4b. Por eso, quien no manda `solo` se comporta igual.

### Variable para pruebas: `AGMEN_NET_IP`

`AGMEN_NET_IP=<ip>` es **una variable para pruebas**, no para uso normal. En modo
red, los sockets se abren en esa IP en vez de en todas las interfaces, y los envíos a
otra computadora salen desde ella. Sirve para tener "dos computadoras" en una sola:
`127.0.0.1` y `127.0.0.2`. Con ella, el hub también acepta una 127.x como
`peer`. Una IP inválida, o que no es de esta computadora, da un error claro al
abrir.

## Conexión con clave: `agmen-agent/1` (desde Agmen 0.14.0)

> **Estado:** implementado en las partes 3b y 3c del RFC 0011 (`python/agmen/agent.py`,
> `agmen agent` y `agmen run`). Diseño y razones en el
> [RFC 0011, §3.4 y §6](rfc/0011-sim-real-y-varias-computadoras.md) (D6, D7 y D9; reto 4).
> La gracia y la reconexión (3c) están en [La gracia y reanudar](#la-gracia-y-reanudar).
> Las pruebas de formato están en `python/test_agent.py`, con los vectores de
> la sección [Vectores](#vectores-de-agmen-agent1).

Es un protocolo de **control** entre dos computadoras del robot, como
`agmen-hub/1`. **No cambia nada de lo de arriba**: el sobre de 64 bytes, los
datos, los topics y `agmen-hub/1` siguen igual. Este protocolo va por su propio
puerto (el de `[[machine]]`, 7391 por defecto). El agente solo arranca nodos de
**su propio robot.toml**: la laptop no manda comandos, rutas ni variables de
entorno. Para pruebas y CI hay una variable, `AGMEN_REMOTE_HOSTS` (lista separada
por comas), que trata direcciones de esta computadora como si fueran otra; no
existe para uso normal (ver `python/agmen/machines.py`).

Desde la parte 4b del RFC 0011, las dos computadoras pasan las tablas de sus hubs por
esta conexión (`tablas`, ver [El puente de topics](#el-puente-de-topics-tablas-desde-0140))
cuando las dos dicen que lo saben hacer. Lo que no cambia: la clave, la firma, los
números `n` y el formato de la línea.

### Texto y líneas

- Todo es texto UTF-8. Cada línea termina en `\n`; un `\r` antes se ignora al leer.
- Líneas del saludo: como máximo 1 KiB.
- Líneas firmadas: como máximo 64 KiB (65 536 bytes, sin contar el `\n`).

### La clave y el material de la sesión

| Nombre | Cómo se obtiene |
|---|---|
| clave | 12 caracteres de `abcdefghjkmnpqrstuvwxyz23456789` (sin `i`, `l`, `o`, `0` ni `1`), escritos `xxxx-xxxx-xxxx`. Nunca viaja por la red. |
| `K` | PBKDF2-HMAC-SHA256 de la clave en minúsculas y sin guiones, con sal `agmen-agent/1` (ASCII), 200 000 vueltas y 32 bytes. Se calcula una vez al arrancar. |
| `nc`, `na` | Aleatorios del cliente y del agente: 16 bytes, escritos como 32 caracteres hexadecimales. |
| `S` | Clave de la sesión: HMAC-SHA256(`K`, `agmen-agent/1\|sesion\|` + `nc` + `\|` + `na`). Firma los mensajes. |

Las pruebas de saludo son HMAC-SHA256(`K`, texto), en hexadecimal (64 caracteres):

```
prueba del cliente = HMAC(K, "agmen-agent/1|cliente|" + nc + "|" + na)
prueba del agente  = HMAC(K, "agmen-agent/1|agente|"  + nc + "|" + na)
```

### El saludo (sin firmar)

Son cuatro líneas en el caso bueno:

```
C → A   agmen-agent/1 hola <nc>
A → C   agmen-agent/1 hola <na> <versión de Agmen>
C → A   prueba <prueba del cliente>
A → C   prueba <prueba del agente>
```

La versión de Agmen sale con sus espacios cambiados por `_`. El cliente comprueba
la prueba del agente; si no coincide, la laptop se rinde con `impostor` (eso no
viaja por la red: es solo de la terminal de la laptop).

En lugar de la línea 2 o la 4, el agente puede contestar un error y cerrar:

```
error version <texto>      el protocolo no es agmen-agent/1 (se avisa en las dos terminales)
error protocolo <texto>    el hola o la prueba están mal formados
error clave <texto>        la prueba no es la de este agente
error espera <texto>       demasiados intentos (ver Límites)
```

- Si la primera línea no empieza con `agmen-agent/` (por ejemplo, una petición
  HTTP), el agente cierra sin contestar.
- Cuando el agente contesta `error espera demasiadas conexiones…` antes del
  saludo, el cliente lo lee igual que `error espera`.
- El cliente solo reconoce los códigos `clave`, `espera`, `version` y `protocolo`.
  Cualquier otro se toma como `protocolo`.
- `error espera` no dice que la clave esté mal, y la laptop no debe decirlo.

### Mensajes firmados

Después de la prueba del agente, cada línea tiene este formato:

```
<n> <json> <firma>
```

- `n`: número decimal que empieza en 1 y sube de uno en uno. Cada sentido lleva
  su propio contador.
- `json`: un objeto en una sola línea, con `"op"`. Se escribe sin espacios
  (`separators=(",", ":")`) y sin escapar las letras no ASCII (`ensure_ascii=False`).
- `firma`: 64 caracteres hexadecimales de HMAC-SHA256(`S`, `<sentido>|<n>|<json>`).
  El sentido es `c` (cliente → agente) o `a` (agente → cliente).

Quien recibe comprueba, en este orden: el formato de la línea, que `n` sea el
siguiente, que la firma sea buena (comparación de tiempo constante), y que el
JSON sea un objeto con `"op"`. Si algo falla, cierra la conexión.

### Mensajes de la laptop al agente

- `sesion`: es el primer mensaje, y solo uno.
  - `perfil`: el perfil activo (texto).
  - `reloj`: `real`, `sim` o `replay`.
  - `agmen`: la versión de Agmen de la laptop.
  - `maquina`: el nombre de su `[[machine]]`.
  - `tablas`: `1` si la laptop tiene hub y pide el puente de topics. Solo va si la
    laptop tiene hub (`[hub] enabled = false` no lo manda). Desde la 4b.
  - `desde`: cómo se llama la laptop para las herramientas del robot (texto; solo va
    con `tablas`). Reglas en [El puente de topics](#el-puente-de-topics-tablas-desde-0140).
  - `sonda`: `1` si la laptop va a hacer la prueba de red con este agente (solo va con
    `tablas`; con `--no-net-check` no va). Desde la 4c; ver
    [La prueba de red](#la-prueba-de-red-sonda-desde-0140).
  - `nodos`: objeto de nombre → lo escrito de ese nodo, con las claves `cmd`,
    `env`, `cwd`, `restart`, `autostart` y `profile`. Una clave que no está
    escrita no aparece.
  - `no_arrancar`: lista de nombres de `nodos` que el agente **no** arranca solo.
    La laptop los arranca después, cuando todas las computadoras aceptaron.
- `arrancar`, `detener`, `reiniciar`: `nodo` (el nombre).
- `detener_todo`, `adios`: sin campos. El `adios` de la laptop se manda al
  terminar a propósito; si el agente ya se despidió, se manda como respuesta
  (ver [Despedida del agente](#despedida-del-agente)).
- `ping`: `t` (un número). El agente lo repite en `pong`. El primer `ping` de una
  conexión reanudada se manda en seguida, sin esperar los 2 s: es el que confirma
  la reanudación.
- `reanudar`: `sesion` (el identificador que dio `sesion_ok`) y `logs`
  (`{nodo: k}`, el último `n` de `log` que la laptop recibió de ese nodo; `"agmen"`
  también cuenta; un nodo que no aparece vale 0). Solo como primer mensaje de una
  conexión nueva, para volver a una sesión que sigue en gracia (ver
  [La gracia y reanudar](#la-gracia-y-reanudar)).

Ejemplo de un `reanudar` (la firma va al final, aquí se omite):

```
1 {"op":"reanudar","sesion":"<32 hex>","logs":{"imu":412,"agmen":3}} <firma>
```

Ejemplo de un `sesion` (la firma va al final de la línea, aquí se omite):

```
1 {"op":"sesion","perfil":"real","reloj":"real","agmen":"0.13.0","maquina":"robot","nodos":{"motor":{"cmd":"python latido.py motor","restart":"on-failure"}},"no_arrancar":["motor"]} <firma>
```

### Mensajes del agente a la laptop

- `sesion_ok`: `maquina`, `agmen` (la versión del agente), `nodos` (la lista de
  nombres, en el orden del archivo), `sesion` (32 caracteres hexadecimales al
  azar, para reanudar) y `gracia` (los segundos de gracia del agente: 10). Después
  de esto, el agente arranca sus nodos con `autostart`.
  - Desde la 4b, además: `tablas: 1` si el agente va a hacer el puente de topics, o
    `sin_tablas: "<motivo corto>"` si la laptop lo pidió y aquí no se puede (por
    ejemplo, sin hub, con IPv6 o con un `desde` inválido). Si no dice nada, es un
    agente anterior.
  - Desde la 4c, además: `sonda: 1` junto a `tablas: 1`, si el agente hace la prueba de
    red con esta laptop (ver [La prueba de red](#la-prueba-de-red-sonda-desde-0140)).
    Con `sonda` y sin haber contestado antes `sonda_espera`, la laptop no cuenta
    ninguna prueba (es un agente que no esperó a la prueba).
- `sesion_error`: `codigo` y `texto`. El agente cierra después de mandarlo.
  - `ocupado`: ya hay otra laptop conectada (el texto dice desde qué IP). Si esa
    laptop se cortó y el agente la tiene en gracia, el texto dice cuántos segundos
    le quedan (`le quedan N s`).
  - `sesion`: un `reanudar` cuyo `sesion` no es el de la sesión en gracia, o no hay
    sesión en gracia. El texto dice que la sesión ya terminó y que sus nodos están
    detenidos: la laptop abre una sesión nueva.
  - `maquina`: el nombre de `maquina` no es el de este agente.
  - `distinto`: un nodo o el reloj no coinciden con el robot.toml del agente. El
    texto trae **todas** las diferencias.
  - `perfil`: el agente no tiene ese perfil, o el perfil dice `local = true`.
  - `nodos_ajenos`: ya corren nodos en esta computadora que el agente no lanzó
    (por ejemplo, un hub con nodos de otra sesión).
  - `antes`: falló `[robot] before`. Su salida llegó antes, como `log`.
  - `config`: el robot.toml del agente tiene un error.
  - `sonda`: la laptop dejó de contestar mientras el agente esperaba su `sigue` (la
    prueba de red). No se preparó nada, y el agente queda libre. Desde la 4c.
  - `protocolo`: campos inválidos, lo primero no fue `sesion`, o `no_arrancar`
    nombra nodos que no son del agente.
  - `interno`: falló algo inesperado en el agente.
- `estado`: `nodo`, `estado` (`corriendo`, `detenido`, `falló`, `reiniciando` o
  `terminó`), `pid` y `codigo`. Solo llega después de `sesion_ok`, en cada cambio.
- `log`: `nodo`, `n` y `linea`.
  - `nodo` es el nombre del nodo. Las líneas del propio agente (y de su
    `before`) van con `"agmen"`; las del hub van con `"agmen"` y el prefijo `hub: `.
  - `n` cuenta las líneas de cada nodo, de uno en uno. Si hay un hueco, el agente
    descartó líneas (ver Límites).
  - `linea` tiene como máximo 8 KiB: una línea más larga se corta.
- `error`: `texto`. Una orden que no se pudo cumplir: nodo desconocido, orden que
  llega mientras el agente todavía prepara la sesión, orden desconocida o más de
  64 órdenes pendientes. Desde la 4c, también cualquier orden que no sea `ping`,
  `adios`, `sonda` o `sigue` mientras el agente espera la prueba de red.
- `pong`: `t`. Repite el `t` del `ping` si es un número o un texto de hasta 64
  caracteres; si no, `null`.

- `reanudar_ok`: `nodos` (como en `sesion_ok`, con el estado actual de cada nodo),
  `gracia`, y `perdidos` solo si hay alguno: `{nodo: cuántas}`, las líneas de `log`
  que el agente ya no tenía. Después van los `log` guardados con `n` mayor que el
  `k` de la laptop, en orden; luego todo sigue como en una sesión normal.
  Desde la 4b, `tablas` y `sin_tablas` van igual que en `sesion_ok`. Desde la 4c,
  `sonda: 1` va junto a `tablas`, si el agente hace la prueba de red (ver
  [La prueba de red](#la-prueba-de-red-sonda-desde-0140)).
- `adios`: `motivo` (texto corto, por ejemplo `Ctrl+C` o `SIGTERM`). Ver
  [Despedida del agente](#despedida-del-agente).

### Quién cierra la conexión

| Qué pasa | Qué hace el agente |
|---|---|
| Línea que no es `<n> <json> <firma>`, firma mala, `n` fuera de orden, JSON inválido, o más de 64 KiB | Cierra y detiene los nodos **en el acto**. Sin gracia: es un error o un ataque, no una red caída |
| `adios` de la laptop | Termina la sesión, detiene los nodos y cierra. Sin gracia |
| Ctrl+C o SIGTERM en el agente | Detiene los nodos, manda su [`adios`](#despedida-del-agente) y cierra. Sin gracia |
| Silencio de 6 s sin ninguna línea (ni un `ping`), cierre sin `adios`, o no se le puede mandar a la laptop | **Entra en gracia**: los nodos siguen 10 s (ver [La gracia y reanudar](#la-gracia-y-reanudar)) |
| Error interno del agente | Detiene los nodos y cierra. Sin gracia |
| `sesion_error`: después de mandarlo | Cierra la conexión |
| Saludo que no termina en 10 s, o cualquier error del saludo | Cierra (todavía no hay sesión) |
| Silencio de 6 s mientras espera el `sigue` de la prueba de red (`sonda_espera`) | Manda `sesion_error` `sonda` y queda libre. **No** entra en gracia: no corrió nada que cuidar |
| Orden desconocida (`op` que no existe) | Contesta `error` |
| `arrancar`, `detener` o `reiniciar` de un nodo que no existe | Contesta `error` |
| Una segunda `sesion` en la misma conexión, o más de 64 órdenes pendientes | Contesta `error` |
| Un envío de `tablas` que falla de otra forma que "mensaje demasiado grande" (p. ej. se agota el tiempo con la línea a medias) | Se trata como una pérdida normal: gracia y reintentos. Nunca se escribe otra línea detrás de una a medias |

La laptop manda `ping` cada 2 s desde que manda `sesion`, así que 6 s de silencio
sí quiere decir que se perdió la conexión.

### La gracia y reanudar

Cuando se corta la red, el agente no detiene los nodos de inmediato. Da una
**gracia** de 10 s (`GRACE`), contados desde la última línea que recibió de la
laptop. Así un corte corto no reinicia nada, y una laptop apagada no deja el robot
corriendo para siempre. La gracia no se configura.

- **Entrar en gracia.** Los nodos y el hub siguen corriendo. El agente lo dice una
  vez, por ejemplo:
  `la laptop (192.168.1.7) dejó de contestar: sus nodos siguen 3 s más por si vuelve (no dijo nada en 6 s)`.
  Si la conexión se cerró sin `adios`, el paréntesis dice `cerró la conexión sin despedirse`.
- **Durante la gracia**, el agente sigue aceptando conexiones:
  - `reanudar` con el `sesion` de la sesión en gracia: se reanuda (abajo).
  - `sesion` de otra laptop: `sesion_error` `ocupado`, con los segundos que quedan.
  - `reanudar` con otro `sesion`, o sin sesión en gracia: `sesion_error` `sesion`.
- **Si la gracia vence**, el agente detiene los nodos y el hub, lo dice y la sesión
  termina: `la laptop (192.168.1.7) no volvió en 10 s: detuve los nodos`. Si la
  laptop intentó volver pero no confirmó que la oye, el texto es
  `la laptop (192.168.1.7) intentó volver pero no confirmó que me oye: detuve los nodos`.
  Los nodos no vuelven a arrancar solos.

**Reanudar.** La laptop manda `reanudar` (ver arriba). El agente contesta `reanudar_ok`
y manda los `log` que la laptop no tiene. Los `estado` no se guardan: `reanudar_ok`
trae el actual.

- **Memoria para reanudar.** El agente guarda las últimas 2 000 líneas de `log` de la
  sesión, con tope de 1 MiB (`RESUME_LOG_LINES` y `RESUME_LOG_BYTES`). No solo las de
  la gracia: una línea mandada justo antes del corte pudo no llegar. Lo que ya no
  está se cuenta en `perdidos`, por nodo, y el cliente lo dice.
- **`reanudar` con `logs` de más de 1 024 nodos** (`RESUME_MAX_NODES`): `sesion_error`
  `protocolo`, y la conexión se cierra.
- **Reanudada sin confirmar.** La reanudación no cancela la gracia. Se confirma con el
  primer mensaje firmado que llega por la conexión nueva: el `ping` que la laptop
  manda en cuanto la instala. Solo entonces el agente cancela el temporizador. Así
  una laptop que no recibe lo que manda el agente no puede alargarle la gracia.
- **Prórroga.** Si el `reanudar` llega con menos de 1 s (`RESUME_CONFIRM`) para que
  venza la gracia, esta se prorroga hasta 1 s desde ese momento. Solo una vez por
  gracia: un `reanudar` repetido sin confirmar no vuelve a moverla.
- **Otro `reanudar`** mientras hay una reanudación sin confirmar reemplaza a la
  conexión anterior, con las mismas reglas.
- **Si la gracia vence en el mismo instante** en que llega la confirmación, la sesión
  termina y no se contesta nada.

**La laptop, en el mismo caso.** Tiene dos cuentas del tiempo. La **pesimista** es lo
último que se sabe que el agente oyó (el momento del último `ping` que tuvo `pong`);
con ella dice "sus nodos siguen N s", y nunca promete de más. La **segura** es lo
último que mandó por una conexión instalada, más la gracia, la prórroga de 1 s, la
espera de 5 s que puede tardar en detener un nodo y 1 s de margen; con ella dice
"habrá detenido sus nodos". Un `reanudar` no mueve la segura. Es una presunción con
margen, no una garantía: sin conexión no hay forma de saberlo.

| Lado de la laptop (`agmen run`) | Valor |
|---|---|
| Intento de reconexión | cada 1 s (`RETRY_EVERY`), hasta Ctrl+C |
| Tope para abrir la conexión TCP en un intento | 2 s (`RETRY_CONNECT_TIMEOUT`) |
| Pausa tras un rechazo (clave, `ocupado`, `distinto`…) | 5 s (`RETRY_REFUSED`) |
| Espera del `pong` de confirmación | 1 s + 6 s (`RESUME_CONFIRM` + `SILENCE_TIMEOUT`); si no llega, el intento falla |
| Recordatorio tras pasar la gracia | cada 60 s (`RETRY_NOTE_EVERY`) |
| Gracia de la cuenta pesimista | 10 s desde el último contacto confirmado |
| Gracia de la cuenta segura | último envío por una conexión instalada + 10 s + 1 s + 5 s (`STOP_BUDGET`) + 1 s (`NET_MARGIN`) |

### Despedida del agente

Cuando el agente termina a propósito (Ctrl+C, Ctrl+Break o SIGTERM) y ya detuvo sus
nodos, manda `{"op":"adios","motivo":"Ctrl+C"}` (o el motivo que corresponda) justo
antes de cerrar. Es con cierre ordenado y un plazo corto (`BYE_TIMEOUT`, 0.5 s). Es de
cortesía: si no llega, la laptop lo trata como una pérdida normal, con gracia.

- También se manda si lo cierran mientras prepara la sesión (antes de `sesion_ok`).
  Entonces la laptop no arranca ningún nodo y sale con código 2.
- No se manda al vencer la gracia, con un mensaje inválido ni con un error interno.
- La laptop contesta con su propio `adios` antes de cerrar. Si el agente lo mandó
  con nodos vivos por error, la sesión termina en el acto y no se queda en gracia.
- Con los nodos ya detenidos, la laptop no dice "sus nodos siguen N s": dice que
  cerraron el agente y que sus nodos están detenidos, y reintenta por si vuelve a
  arrancar. Al volver, abre sesión nueva y ningún nodo arranca solo.

### El puente de topics (`tablas`, desde 0.14.0)

> **Estado:** implementado en la parte 4b del RFC 0011: `python/agmen/tables.py` (el
> puente), con sus enganches en `python/agmen/agent.py` (`agmen agent`) y en `agmen run`.
> Las pruebas están en `python/test_tablas.py`. Diseño y razones en el
> [RFC 0011](rfc/0011-sim-real-y-varias-computadoras.md), §3.5, §8 y §9.

Con esto, un topic cruza de verdad entre la laptop y cada robot. El puente pasa las
tablas de los dos hubs **por esta misma conexión**, firmada igual que el resto. Nada
nuevo escucha en la red: cada lado habla con su propio hub por `127.0.0.1`, como
cualquier herramienta.

#### Cuándo hay puente

Las dos computadoras tienen que decirlo:

- La **laptop** (`agmen run`) lo dice en `sesion` con `tablas: 1` y `desde`, solo si
  tiene hub. Con `[hub] enabled = false`, no lo dice.
- El **agente** (`agmen agent`) lo dice en `sesion_ok` o `reanudar_ok` con `tablas: 1`,
  solo si va a hacer el puente: tiene hub, `desde` es válido y la laptop llega por IPv4.
  Si la laptop lo pidió y aquí no se puede, dice `sin_tablas` con el motivo.

Mientras la conexión está **confirmada** (ver [La gracia y reanudar](#la-gracia-y-reanudar)),
cada lado hace tres cosas, una vez por segundo o cada 2 s:

1. **Enciende el modo red de su hub**: `red peers=<nombre>@<ip>` cada 2 s. En la laptop,
   el nombre es el de su `[[machine]]`. En el robot, es el `desde` de la laptop. La IP
   es la que **este** lado ve del otro.
2. **Exporta lo suyo**: lee su hub (`query`) y manda al otro sus endpoints **locales**:
   todos los Publisher y los Subscriber con `net=1`. Nunca reexporta lo que vino de
   otra computadora (no hay bucles).
3. **Importa lo del otro**: registra cada endpoint del otro en su hub como entrada de
   otra computadora (`sub`/`pub` con `machine=`), con id `<computadora>.<id>`, la IP que
   ve este lado y los latidos de siempre. Lo que ya no viene en la tabla nueva se quita.

Si se pierde la conexión, el puente deja de latir lo del otro. Esas entradas caducan en
3 s y el hub vuelve a modo local en 6 s. La tabla importada se descarta. Al reconectar
(también al reanudar), todo vuelve solo, sin reiniciar ningún nodo.

#### El mensaje `tablas`

```
1 {"op":"tablas","n":3,"parte":1,"de":2,"pubs":[{…}],"subs":[{…}]} <firma>
```

(El primer número de la línea es el de la conexión, como en todos los mensajes. El `n`
del mensaje es el de la tabla.)

- `n`: número de la tabla. Sube con cada tabla nueva.
- `parte` y `de`: la parte que es (desde 1) y cuántas son. Una tabla vacía va con `de`
  igual a 1 y las listas vacías.
- Cada parte trae **como mucho 100 endpoints** y **48 KiB** en JSON. La línea firmada
  tiene su tope de 64 KiB, así que las tablas grandes van en varias partes.
- Una tabla **vale cuando llegaron todas sus partes**. Mientras tanto, sigue valiendo la
  anterior. Una tabla nueva **sustituye** a la anterior, nunca se suman.
- Campos de un endpoint de `pubs`: `id`, `node`, `pid`, `topic`, `schema` (16
  dígitos hexadecimales), `type` y `text`. De `subs`: `id`, `node`, `pid`, `topic` (puede
  ser `*`), `schema` (16 hexadecimales o `*`), `type`, `port` (1 a 65535) y `tcp` (0 a
  65535). Los puertos son enteros. Los `id` van de 1 a 64 letras, dígitos, `_` o `-`
  (sin punto: el punto lo pone quien recibe, en `<computadora>.<id>`).

**Quien recibe** valida cada endpoint como lo haría el hub. Lo inválido se descarta, y
si un `id` se repite en la tabla, se queda el primero. La **IP** la pone quien recibe:
la del otro lado tal como la ve su conexión. Nunca usa una dirección que venga en el
mensaje.

**Quien manda** sigue estas reglas:

- Una tabla pendiente de salir se **sustituye** por la más nueva; no se acumulan.
- Las partes de tabla no cuentan para descartar `log` en la cola de salida del agente.
- Al cerrar (Ctrl+C), `detener_todo` y `adios` no esperan detrás de una tabla. Una
  parte que ya salió puede retener esas órdenes hasta 6 s, el tiempo de silencio.
- Un envío de tabla que falla de otra forma que "mensaje demasiado grande" cierra la
  conexión para todos. Se trata como una pérdida normal (gracia y reintentos). Nunca se
  escribe otra línea detrás de una a medias.

#### Límites de las tablas

| Qué | Valor |
|---|---|
| Endpoints por parte | 100 (`PART_MAX`) |
| Tamaño de una parte, en JSON | 48 KiB (`PART_BYTES`) |
| Endpoints por tabla | 1 000 (`MAX_ENDPOINTS`). Lo que sobra no pasa, y se dice |
| Bytes por tabla, ya escapados como van al hub | 1 MiB (`TABLE_MAX_BYTES`). Lo que pasa se descarta y se dice |
| Una línea de endpoint para el hub | 65 507 bytes (`HUB_LINE_MAX`). Un endpoint más grande se descarta y se dice |


#### Avisos y casos de borde

- **Un aviso por tipo de problema**, no uno por endpoint: cuántos y un ejemplo. Si hay
  cientos de endpoints rechazados, se dice una vez.
- **El hub no contesta a la consulta** del puente: se dice una vez, y el puente sigue
  intentando.
- **Lista de su hub incompleta** (faltan partes de `state`): el puente lo dice una vez
  por cambio de estado, como mucho uno cada 30 s, con `la tabla de mi hub no cabe en
  una respuesta…`. No vacía la tabla: sigue mandando la última buena hasta 3 consultas
  incompletas seguidas. A la cuarta, manda la tabla vacía y avisa. Cuando la lista
  vuelve completa, lo dice con `ya cabe`.
- **Dos robots**: la laptop no pasa a un robot lo que es de otro. Si un topic (no
  comodín) se publica en un robot y se escucha en otro, `agmen run` lo avisa **una vez
  por topic**: `'<topic>' se publica en '<A>' y se escucha en '<B>': en esta versión dos
  robots no se pasan topics entre sí`. Pon uno de los dos nodos en la laptop.
- **Una IP que no es IPv4** no sirve en esta versión: se avisa y no hay puente.
- **Tabla que llega sin puente** (de un lado que no lo espera): se ignora.

#### Simulación y reproducción

Si la laptop pidió una sesión con `reloj` = `sim` o `replay` (`agmen run --sim` o
`--play`), en el **robot** solo cruzan los nodos que arrancó el agente. Esto evita que
una simulación mueva el robot de verdad (RFC 0011, §6, reto 1). En `real` no cambia nada.

1. **Exporta solo lo suyo**: un endpoint cuyo `pid` es el de uno de sus nodos o el de
   un descendiente suyo. Si no se puede saber, no se exporta (lado seguro).
2. **Lo que importa de la laptop lo ven solo sus nodos**: las entradas de la laptop
   llevan `solo=<ids>` (ver [Entradas de otra computadora](#entradas-de-otra-computadora-desde-0140)).
   Un Publisher arrancado a mano en el robot no le manda nada a la laptop.
3. **Lo avisa**, una vez por nodo ajeno, en la terminal del robot (y la laptop lo recibe
   como línea del robot): `'<nodo>' (pid N) no lo arranqué yo y esta es una sesión de
   simulación: no lo conecto con <laptop> (ni recibe ni manda). Si es parte del robot,
   ponlo en robot.toml`. En reproducción, dice "reproducción".

Del lado de la laptop no hay restricción: sus herramientas y scripts siguen viendo todo.
Un nodo que se reinicia (pid nuevo) sigue contando: se mira en cada tabla.

Anotado, sin cambio de código:

- Un nodo dentro de un contenedor declara el `pid` de dentro del contenedor. En
  simulación cuenta como ajeno.
- Un descendiente huérfano (su padre ya murió) pasa a ser ajeno.

#### Si no hay puente: qué se dice

- La laptop dice `tablas` y el agente no: `sesion_ok` trae `sin_tablas`, y `agmen run`
  avisa `no pasa topics entre computadoras (<motivo>)`. Los motivos cortos son, por
  ejemplo: sin hub en el robot (`[hub] enabled = false`), IPv6, o un `desde` inválido.
- El agente no dice nada (es anterior a la 4b): `agmen run` avisa `su agmen agent no dice
  'tablas': es un Agmen anterior`, y pide `pip install -U agmen` en esa computadora.
- En los dos casos, los nodos siguen corriendo como en la 3c. Solo no cruzan los topics.

#### En pruebas: `AGMEN_NET_IP`

Con `AGMEN_NET_IP`, la conexión de `agmen run` con el agente sale desde esa IP, para
que el agente vea a la laptop en la IP donde escuchan sus nodos. Es una variable para
pruebas (ver [Variable para pruebas](#variable-para-pruebas-agmen_net_ip)). Sin ella,
la conexión sale como siempre.

### La prueba de red (`sonda`, desde 0.14.0)

> **Estado:** implementada en la parte 4c del RFC 0011: `python/agmen/sonda.py` (los
> sockets y el formato de la línea), con sus enganches en `python/agmen/agent.py`
> (`agmen agent`) y `python/agmen/cli.py` (`agmen run`). Las pruebas están en
> `python/test_sonda.py`. Diseño y razones en el
> [RFC 0011](rfc/0011-sim-real-y-varias-computadoras.md), §3.5 y §6 (reto 2), y en su
> sección 5 (el orden: la prueba va antes de que el robot prepare nada).

Los topics van directo de nodo a nodo: UDP para los mensajes chicos y una conexión TCP
para los grandes, a puertos cualquiera del otro lado. La prueba de red comprueba justo
eso, en los dos sentidos, antes de arrancar nada. Así un firewall que bloquea el paso se
dice al conectar, y no como topics que nunca llegan.

**Cuándo hay prueba.** Solo cuando va a haber puente (los dos lados dicen `tablas`) y la
laptop dice `sonda: 1` en `sesion`. El agente lo dice en `sesion_ok` y en `reanudar_ok`,
junto a `tablas`, solo si hace la prueba. Sin puente no hay prueba.

**Capacidad.** `"sonda": 1` es un campo nuevo y añadido, como `tablas`. Quien no lo
conoce lo ignora, y la conexión sigue como en la 4b.

#### El orden al conectar

| # | Quién | Mensaje | Qué pasa |
|---|---|---|---|
| 1 | laptop → agente | `sesion` (con `tablas`, `desde` y `sonda`) | Como siempre |
| 2 | agente → laptop | `sonda_espera` | El agente **no** corre `[robot] before`, ni arranca su hub, ni ningún nodo. Lo dice en su terminal |
| 3 | laptop → agente | `sonda` (con `id`, `udp`, `tcp`, `dato`) | La laptop abrió sus sockets y da sus puertos y su dato |
| 4 | agente → laptop | (datos) y luego `sonda_ok` | El agente abre sus sockets, manda su dato a los dos puertos de la laptop (UDP ×3, TCP ×1) y contesta `sonda_ok` con los suyos |
| 5 | laptop → agente | (datos) | La laptop manda su dato a los dos puertos del agente |
| 6 | agente → laptop | `sonda_fin` | Al cabo de hasta 2 s, dice lo que **él** recibió de la laptop |
| 7 | laptop | — | Junta los cuatro resultados de cada robot. Si alguno falla, ningún robot recibe `sigue` (ver abajo) |
| 8 | laptop → agente | `sigue` | Solo cuando pasó con **todos** los robots, a todos a la vez |
| 9 | agente | `before`, comprobación de nodos ajenos, hub, `sesion_ok` | Lo de siempre, ahora sí. `sesion_ok` lleva `tablas` y `sonda` |

Si la laptop decide no seguir, manda `adios` (o cierra) y el agente termina sin haber
preparado nada.

#### Mensajes

Todos van firmados como el resto de `agmen-agent/1`. Ninguno tiene campos fuera de los
de esta tabla.

| Mensaje | Campos | Quién lo manda |
|---|---|---|
| `sonda_espera` | (ninguno) | agente |
| `sonda` | `id`: entero de 0 a 2 147 483 647 · `udp`, `tcp`: puertos de 1 a 65 535 · `dato`: 32 caracteres hexadecimales (minúsculas) | laptop |
| `sonda_ok` | `id` (el de su `sonda`) · `udp`, `tcp`: puertos del agente · `dato`: 32 hex, el del agente · `so`: `linux`, `windows` o `darwin` | agente |
| `sonda_fin` | `id` · `udp`, `tcp`: `true` o `false`, lo que el **agente** recibió de la laptop · `error` (solo si no pudo hacer la prueba): texto con el motivo | agente |
| `sigue` | (ninguno) | laptop |

- `id` es el número de cada prueba. `sonda_ok` y `sonda_fin` lo repiten, y la laptop
  descarta las respuestas de otra prueba (por ejemplo, de una vieja en la misma conexión).
- Si el agente no puede hacer la prueba (un `sonda` mal formado, no pudo abrir sus
  sockets, ya hay otra prueba por esa conexión), manda `sonda_fin` con `udp` y `tcp` en
  `false` y `error` con el motivo corto. La laptop falla la prueba en ese momento, con
  ese motivo, sin esperar el resto.
- Si el agente recibe un `sigue` sin haber mandado `sonda_espera`, contesta `error`.

#### El dato en la red

La línea que viaja por UDP y por TCP es texto, y no va firmada:

```
agmen-sonda/1 <dato>\n
```

Son 47 bytes: 14 del prefijo (`agmen-sonda/1 `), 32 hex del dato y el `\n`. Por UDP se manda
el datagrama entero, tres veces, separadas 0,1 s. Por TCP se abre una conexión, se manda
la línea y se cierra. Solo cuenta si el dato es el esperado **y** viene de la IP del otro
lado. Lo demás se ignora, sin avisar por paquete.

Las IP son las que ve cada lado en la conexión firmada: nunca se usa una dirección que
venga en un mensaje. Solo IPv4.

#### Tiempos y límites

| Qué | Valor |
|---|---|
| Espera de cada lado por lo del otro (`SONDA_S`) | 2 s. Termina antes si ya llegó todo |
| Plazo de la laptop para tener `sonda_ok` y `sonda_fin` (`SONDA_S` + `MARGEN_S`) | 4 s desde que manda `sonda`. Si no llegan, la prueba falla con "no contestó la prueba de red" |
| Datagramas UDP por prueba | 3, separados 0,1 s |
| Conexiones TCP abiertas a la vez en el escucha | 8 (las demás se cierran) |
| `ping` de la laptop durante la espera de `sigue` | cada 2 s, desde `sonda_espera` hasta `sigue` o `adios` |
| Silencio que da por perdida la espera (de cualquiera de los dos lados) | 6 s (`SILENCE_TIMEOUT`). Es el mismo de siempre |
| Aviso en la terminal del agente mientras espera `sigue` | cada 60 s (`AVISO_ESPERA_S`), una línea |
| Espera del `sigue` | Sin tope fijo: dura mientras la laptop siga mandando `ping`. Puede tardar minutos si la laptop abre antes otra computadora (que corre su `before` primero) |

**Varios robots.** La laptop hace las pruebas de todos a la vez. No manda `sigue` a
ninguno hasta tener el resultado de todos. Si uno falla al conectar, manda `adios` a
todos y no arranca nada. Un robot que espera `sigue` mientras la laptop abre la sesión
con otro sigue esperando, mientras la laptop le mande `ping`.

**Al reanudar o reconectar.** La prueba se repite, pero solo avisa si falla (ver el
siguiente bloque). Nunca detiene ni cierra nada.

#### Qué pasa con el resultado

- **Al conectar** (antes de arrancar nada): si algún resultado es falso, `agmen run` no
  arranca nada, despide a los agentes y sale con código 2, igual que cuando un agente no
  contesta. El mensaje dice, por cada sentido que falla, qué computadora no recibe de
  cuál, si es por UDP, por TCP o por los dos, y qué hacer según el sistema de la que no
  recibe.
- **Al reanudar o reconectar** (los nodos ya corren): si falla, solo avisa, una vez por
  cambio de estado. Cuando vuelve a pasar, lo dice en una línea. No detiene nada y no
  cierra la conexión.
- **Un fallo a medias** (UDP sí y TCP no, o al revés) cuenta como fallo, y dice cuál de los
  dos no pasa.

#### Compatibilidad

| Quién | Qué pasa |
|---|---|
| Laptop sin `sonda` (anterior a la 4c, o con `--no-net-check`) | El agente no hace la prueba: contesta `sesion_ok` directo, como en la 4b. Nada cambia |
| Agente anterior a la 4c (no dice `sonda`) | La laptop no recibe `sonda_espera`: lo dice una vez (es un Agmen anterior) y la sesión sigue. No es un fallo. Ese agente ya corrió su `before` y su hub, porque no sabe esperar |
| Agente que no hace el puente (sin hub, IPv6, `desde` inválido) | `sesion_ok` con `sin_tablas`, sin `sonda_espera`. No hay prueba |
| Sin puente (laptop sin hub) | No hay `sonda` |
| Sesión nueva tras perder la gracia (reintento) | El agente también espera `sonda_espera`. La laptop hace la prueba y manda `sigue` **pase o no pase**, porque los nodos de la laptop ya corren. Si falló, lo dice una vez |

Lo que no cambia: el sobre, la clave, la firma, los números `n` y el formato de cada
línea firmada.

#### Nota de seguridad

La línea de la prueba (`agmen-sonda/1 <dato>`) va por UDP y TCP **sin cifrar ni firmar**:
el `dato` viaja en claro. Quien esté en el camino podría leerlo y mandarlo él mismo, y así
hacer pasar la parte UDP de la prueba aunque el firewall la bloquee. La prueba detecta
firewalls que bloquean el paso. **No** detecta a un atacante en la red. Los datos de los
topics no cambian por esto: la firma de `agmen-agent/1` protege la conexión de control.

### Compatibilidad del puente (desde 0.14.0)

- **Un lado sin `tablas`** (versión anterior a la 4b): no hay puente y no se rompe nada.
  Los nodos corren, la conexión funciona y los topics no cruzan. Se dice una vez, con
  qué hacer. Actualicen las dos computadoras a la misma versión.
- **Un `tablas` que llega sin puente** se ignora sin decir nada.
- **Una laptop sin hub** no dice `tablas`: el agente no hace puente, y no dice nada.

### Límites

| Qué | Valor |
|---|---|
| Pruebas malas (`error clave`) por minuto, por IP (en IPv6, por cada /64) | 5 |
| Pruebas malas por minuto, en total | 60. Las pruebas malas de la IP de la última sesión aceptada sí suman, pero a esa IP no se le aplica este bloqueo |
| Pausa tras una prueba mala, por IP | 1 s |
| Conexiones sin saludar a la vez, por IP | 4 |
| Conexiones sin saludar a la vez, en total | 32 (una queda reservada para la IP de la última sesión aceptada) |
| Tiempo para completar el saludo | 10 s |
| Silencio antes de dar la conexión por perdida (entra en gracia) | 6 s |
| Gracia: sin laptop, sus nodos siguen esto desde lo último recibido de ella | 10 s (`GRACE`) |
| Prórroga de una reanudación sin confirmar, una vez por gracia | 1 s (`RESUME_CONFIRM`) |
| Memoria para reanudar, por sesión | 2 000 líneas de `log` y 1 MiB (`RESUME_LOG_LINES`, `RESUME_LOG_BYTES`) |
| Nodos en `logs` de un `reanudar` | 1 024 (`RESUME_MAX_NODES`) |
| Despedida del agente (`adios`), plazo de envío | 0.5 s (`BYE_TIMEOUT`) |
| `ping` de la laptop | cada 2 s |
| Órdenes pendientes por sesión | 64 |
| Línea firmada | 64 KiB |
| Línea de log | 8 KiB |
| Cola de salida del agente, para los `log` | 5 000 mensajes o 4 MiB. Lo que no cabe se descarta y su `n` sigue subiendo |
| Respuestas (`pong`, `error`) sin leer por la laptop | 1 000. Si pasan 6 s sin que las lea, el agente cierra la sesión |
| Avisos en la terminal del agente por conexiones sin clave | Uno por IP y tipo, por minuto; 20 por minuto en total |

### Compatibilidad

- **Cliente de la 3b con agente de la 3c.** El cliente de la 3b ignora lo que no
  conoce: `sesion_ok` con campos nuevos, y el `adios` del agente (no hace nada con
  él). Al cerrarse la conexión, lo trata como antes: `agmen run` detiene todo. El
  agente nuevo no sabe qué versión es la laptop, así que aplica la gracia igual.
- **Agente de la 3b con cliente de la 3c.** El agente de la 3b no manda `sesion` ni
  `gracia`, así que el cliente nuevo no puede reanudar: al volver, abre una sesión
  nueva. Como no recibe la gracia, la toma como cero: nunca dice "sus nodos siguen
  N s" (ese agente detiene sus nodos en cuanto pierde a la laptop), sino que puede
  que ya los haya detenido. Aun así, usen la misma versión de Agmen en las dos
  computadoras.
- Los mensajes nuevos (`reanudar`, `reanudar_ok`, `adios`) van firmados igual que los
  de antes, con el mismo `n` y la misma forma de la línea. El formato de la red no
  cambió.

### Vectores de `agmen-agent/1`

Clave de prueba `kq3f-9xm2-7tdw`, la de `python/test_agent.py`. **No la uses en un
robot real.** Con `nc = 00112233445566778899aabbccddeeff` y
`na = ffeeddccbbaa99887766554433221100`:

```
prueba del cliente  4ccd04c6bd307ed3e48e8cd315d5e5bb15aac997d193ededeb480773d8c376a9
prueba del agente   db3e2b006ee58e65791487588690824aa08f34c3b3be982036532322666f6cee
S                   5c8b7630ef446344669af3d4e406f590883efd0b94b39afb772f1afde0223989
```

El primer mensaje de la laptop (`n = 1`, sentido `c`) con `{"op":"ping","t":1.5}`:

```
1 {"op":"ping","t":1.5} abf6628a56840d9a62e07d7670936ead562102b9cdf5b14d262b51406242f169
```

Estos valores vienen de la referencia `hmac`/`hashlib` y los comprueban las
pruebas de `python/test_agent.py`. Los mensajes de la 3c no tienen vectores propios:
se firman igual que estos.
