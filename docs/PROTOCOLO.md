# Protocolo de cable de Motio — versión 0

> **Estado:** implementado (Nivel 0). La versión 1 está propuesta en
> [`rfc/0001-formato-de-mensajes.md`](rfc/0001-formato-de-mensajes.md) y la reemplazará.

Este documento es **el contrato entre lenguajes**. Un nodo escrito en Rust,
Python, C++ o en un microcontrolador es compatible si escribe y lee
exactamente estos bytes. El código de Rust (`crates/motio-core/src/wire.rs`) es la
implementación de referencia y `python/motio/wire.py` es la segunda.

## Reglas generales

- Todos los enteros y flotantes van en **little-endian**.
- Los flotantes son IEEE-754 (`f32` = 4 bytes, `f64` = 8 bytes).
- No hay padding: los campos van pegados en el orden indicado.
- Transporte en v0: **un mensaje = un datagrama UDP**.

## Header (32 bytes)

| Offset | Tamaño | Tipo     | Campo         | Descripción                                      |
|-------:|-------:|----------|---------------|--------------------------------------------------|
| 0      | 3      | bytes    | `magic`       | Siempre `"MOT"` (0x4D 0x4F 0x54)                 |
| 3      | 1      | u8       | `version`     | Versión del protocolo. Actualmente `0`           |
| 4      | 8      | u64      | `type_id`     | Hash FNV-1a 64 del esquema del mensaje           |
| 12     | 8      | u64      | `seq`         | Contador del publisher, empieza en 0, +1 por mensaje |
| 20     | 8      | u64      | `stamp_ns`    | Momento de envío, ns desde la época Unix         |
| 28     | 4      | u32      | `payload_len` | Bytes del payload que siguen al header           |
| 32     | N      | bytes    | payload       | Contenido del mensaje según su tipo              |

## type_id

`type_id = FNV1a64(esquema_canónico_en_UTF-8)`

```
hash = 0xcbf29ce484222325
para cada byte b:
    hash = hash XOR b
    hash = (hash * 0x100000001b3) mod 2^64
```

Valores de prueba: `FNV1a64("") = 0xcbf29ce484222325`,
`FNV1a64("a") = 0xaf63dc4c8601ec8c`.

Si se agrega, quita, renombra o cambia de tipo cualquier campo, el esquema
cambia y con él el `type_id`. Así el receptor **detecta y reporta** que el
emisor usa otra versión del mensaje en lugar de leer basura en silencio.

## Validación obligatoria al recibir

Un receptor debe rechazar el paquete y **reportar el motivo** si:

1. mide menos de 32 bytes;
2. `magic` ≠ `"MOT"`;
3. `version` no es soportada;
4. `payload_len` ≠ bytes restantes del datagrama;
5. `type_id` ≠ el tipo esperado;
6. el payload no mide lo que su tipo requiere.

## Tipos de mensaje

### ImuSample

- Esquema: `ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}`
- `type_id`: `0x14626bee23e54e09`
- Payload: 24 bytes

| Offset | Tipo | Campo | Unidad |
|-------:|------|-------|--------|
| 0      | f32  | ax    | m/s²   |
| 4      | f32  | ay    | m/s²   |
| 8      | f32  | az    | m/s²   |
| 12     | f32  | gx    | rad/s  |
| 16     | f32  | gy    | rad/s  |
| 20     | f32  | gz    | rad/s  |

## Ejemplo de lectura en C++ (futuro `#include <motio/motio.hpp>`)

```cpp
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace motio::wire {

// Válido en máquinas little-endian (x86, ARM, ESP32).
struct Header { uint64_t type_id, seq, stamp_ns; uint32_t payload_len; };

inline bool read_header(const uint8_t* buf, size_t n, Header& h) {
    if (n < 32 || std::memcmp(buf, "MOT", 3) != 0 || buf[3] != 0) return false;
    std::memcpy(&h.type_id,     buf + 4,  8);
    std::memcpy(&h.seq,         buf + 12, 8);
    std::memcpy(&h.stamp_ns,    buf + 20, 8);
    std::memcpy(&h.payload_len, buf + 28, 4);
    return h.payload_len == n - 32;
}

}  // namespace motio::wire
```

(Se usa `memcpy` campo por campo y no un `reinterpret_cast` a un struct,
porque el compilador puede meter padding y el acceso desalineado falla en
algunos microcontroladores.)

## Historial

- **v0** — header fijo de 32 bytes, UDP punto a punto, tipos escritos a mano.
