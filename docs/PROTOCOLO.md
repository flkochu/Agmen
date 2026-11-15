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

Hoy cada computadora tiene su propio hub: los topics todavía no cruzan entre
computadoras (bloque 4 del RFC 0011).

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
  64 órdenes pendientes.
- `pong`: `t`. Repite el `t` del `ping` si es un número o un texto de hasta 64
  caracteres; si no, `null`.

- `reanudar_ok`: `nodos` (como en `sesion_ok`, con el estado actual de cada nodo),
  `gracia`, y `perdidos` solo si hay alguno: `{nodo: cuántas}`, las líneas de `log`
  que el agente ya no tenía. Después van los `log` guardados con `n` mayor que el
  `k` de la laptop, en orden; luego todo sigue como en una sesión normal.
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
| Orden desconocida (`op` que no existe) | Contesta `error` |
| `arrancar`, `detener` o `reiniciar` de un nodo que no existe | Contesta `error` |
| Una segunda `sesion` en la misma conexión, o más de 64 órdenes pendientes | Contesta `error` |

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
  arrancar. Al volver, abre sesión nueva y ningún nodo arranca solo.### Límites

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
  nueva. Pero como no recibe la gracia, usa la de 10 s, y dirá "sus nodos siguen N s"
  aunque el agente de la 3b ya los detuvo en el acto. **Límite conocido:** usen la
  misma versión de Agmen en las dos computadoras.
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
