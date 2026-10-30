# Framework de robótica — Nivel 0

Framework propio para reemplazar a ROS 2, construido desde cero y por niveles.
El núcleo está en **Rust**. La compatibilidad con **Python** y **C++** vive en
el protocolo de cable ([`docs/PROTOCOLO.md`](docs/PROTOCOLO.md)): cualquier
lenguaje que lo implemente puede ser un nodo.

> El nombre del proyecto es provisional: los paquetes empiezan con `RFW`
> (Robot FrameWork). Cámbialo cuando elijas uno.

## Estructura

```
framework/
├── Cargo.toml               workspace de Rust
├── crates/
│   ├── wire/                formato binario: header, type_id, mensajes (+ tests)
│   └── nivel0/              binarios publisher y subscriber
├── python/
│   ├── rfw.py               el mismo formato en Python (solo librería estándar)
│   ├── publisher.py
│   └── subscriber.py
└── docs/PROTOCOLO.md        la especificación: el contrato entre lenguajes
```

## Requisitos

- Rust (instálalo con `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`)
- Python 3.10 o más reciente

## Cómo correrlo

```bash
cargo test                         # pruebas del formato
cargo build --release

# Terminal 1
cargo run --release --bin subscriber

# Terminal 2
cargo run --release --bin publisher -- --rate 100
```

Cada segundo el subscriber imprime algo como:

```
[subscriber] rx=  100 perdidos=0  desordenados=0  errores=0  latencia µs: min=28 prom=60 p99=138 max=345
```

### Mezclando lenguajes

```bash
# Rust publica → Python escucha
python3 python/subscriber.py
cargo run --release --bin publisher

# Python publica → Rust escucha
cargo run --release --bin subscriber
python3 python/publisher.py
```

## Qué aprendes en este nivel

| Concepto | Dónde verlo |
|---|---|
| Serialización a mano y endianness | `crates/wire/src/lib.rs` → `Header::write_to` / `read_from` |
| Framing (header + payload con longitud) | campo `payload_len` |
| Detección de pérdidas con números de secuencia | `nivel0/src/lib.rs` → `SeqTracker` |
| Latencia (y por qué solo vale en la misma máquina) | `wire::now_ns` |
| Lazo a frecuencia fija sin acumular error | `publisher.rs` → programación por *deadline* |
| Errores claros en vez de fallas silenciosas | `WireError` + `type_id` |

## Ejercicios (en orden)

1. **Lee el código del crate `wire`** y dibuja a mano los 56 bytes de un `ImuSample`.
2. **Ve un error de tipo.** En `python/rfw.py`, agrega `;f32 temp` al `SCHEMA`
   de `ImuSample`, corre `python/publisher.py` contra el subscriber de Rust y
   lee el error. Luego regrésalo.
3. **Satura el sistema.** Sube el `--rate` del publisher (1000, 10000, 50000)
   y observa la latencia, los `perdidos` y los `retrasos`. ¿Dónde se rompe? ¿Por qué?
4. **Pruébalo en red.** Corre el subscriber en otra computadora con
   `--listen 0.0.0.0:7400` y el publisher con `--to IP_DE_ESA_PC:7400`.
   Compara las pérdidas por cable y por WiFi. (La latencia saldrá rara: los
   relojes de las dos máquinas no están sincronizados. Eso lo resolvemos en el nivel 7.)
5. **Tu primer mensaje.** Crea `MotorCommand { f32 left; f32 right }` en Rust
   y en Python siguiendo el patrón de `ImuSample`, con su test, y agrégalo a
   `docs/PROTOCOLO.md`.
6. **Reto C++.** Escribe un subscriber en C++ usando el ejemplo de `PROTOCOLO.md`.

## Limitaciones conocidas (lo que motiva el Nivel 1)

- Solo **un** subscriber puede escuchar en un puerto. No hay pub/sub real.
- El publisher tiene que saber la IP y el puerto del subscriber: no hay discovery.
- No hay "topics": un puerto equivale a un flujo de datos.
- Cada tipo de mensaje se escribe a mano en cada lenguaje (lo automatiza el Nivel 2).
- Un mensaje no puede pasar de ~64 KB (límite de un datagrama UDP).

## Roadmap

| Nivel | Tema |
|---|---|
| **0** | **Fundamentos: serialización, UDP, seq, latencia** ← aquí |
| 1 | Pub/Sub local: nodos, topics, registro, CLI `list`/`echo` |
| 2 | Sistema de tipos: IDL propio + generador de código (Rust/Python/C++) |
| 3 | Executor: timers, callbacks, modelo de hilos |
| 4 | Request/Reply y acciones |
| 5 | Config y launch con un solo archivo |
| 6 | Red multi-máquina, discovery sin multicast, memoria compartida |
| 7 | Tiempo y transformadas |
| 8 | Microcontroladores (ESP32/STM32) |
| 9 | Abstracción de hardware y control |
| 10 | Grabación, reproducción y visualización |
| 11 | Tiempo real, seguridad, bridge con ROS 2 |

## Reglas de diseño (lo que hacemos distinto a ROS 2)

1. Nunca fallar en silencio.
2. Discovery que funcione en cualquier red, sin depender de multicast.
3. Cero boilerplate.
4. Concurrencia predecible.
5. El tiempo como algo de primera clase.
6. Los microcontroladores son nodos de primera clase.
7. Introspección desde el día 1.
