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
`python/agmen/schema.py`), y las dos se revisan contra los mismos
**vectores de prueba**: [`tests/vectors/v1.txt`](../tests/vectors/v1.txt).

## Reglas generales

- Todos los enteros y flotantes van en **little-endian**.
- Los flotantes son IEEE-754 (`f32` = 4 bytes, `f64` = 8 bytes).
- Transporte en el nivel 1: **un mensaje = un datagrama UDP**, a una dirección
  fija (los topics con descubrimiento llegan en el nivel 2).
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
| 7 | 1 | u8 | `flags` | bit 0 `SHM` (nivel 8), bit 1 `FIN`. El resto en 0 |
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
| 3 | `encoding` > 3 o un bit de `flags` desconocido; `arrow` y `SHM` todavía no están soportados |
| 4 | `payload_len` ≠ bytes restantes |
| 5 | `schema_id` ≠ el que espera el receptor (`TIPO INCOMPATIBLE`, con el esquema que llegó si ya se anunció) |
| 6 | `compact`: el payload no mide lo que dice el esquema |

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
