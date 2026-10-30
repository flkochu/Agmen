# Motio

> *Motio* (latín): movimiento.

Motio es un framework para **robótica + IA + IoT** que se construye desde cero
para reemplazar a ROS 2. Su núcleo es el **sistema nervioso** del robot: mueve
mensajes entre nodos de forma rápida, confiable y con marca de tiempo. La IA,
la visión y el IoT **no viven dentro del núcleo**: se conectan a él como
paquetes opcionales.

- Núcleo en **Rust**; corre en **Windows, Linux y macOS** (verificado por CI).
- **Python de primera clase** y **C++** soportado. La compatibilidad vive en
  el formato de mensajes, no en un lenguaje.

📄 **Lee primero:** [`docs/VISION.md`](docs/VISION.md) (visión, requisitos,
comparación con dora-rs y roadmap) y
[`docs/rfc/0001-formato-de-mensajes.md`](docs/rfc/0001-formato-de-mensajes.md)
(propuesta del formato v1).

## Estado: Nivel 0 ✅ (protocolo v0)

| Nivel | Tema |
|---|---|
| **0 ✅** | **Fundamentos: serialización, UDP, seq, latencia, CI** |
| 1 ⏭ | Sobre v1 + codificación `compact` + esquema en texto + decodificación dinámica |
| 2–13 | Ver [`docs/VISION.md`](docs/VISION.md#6-roadmap-por-niveles) |

## Estructura

```
motio/
├── crates/motio-core/       núcleo en Rust (por ahora: el formato de cable v0)
├── examples/nivel0/         publisher y subscriber del Nivel 0
├── python/
│   ├── motio/wire.py        implementación de referencia en Python (import motio)
│   ├── publisher.py · subscriber.py
│   └── test_wire.py
├── bench/                   mediciones que respaldan decisiones de diseño
├── scripts/interop.py       prueba automática Rust <-> Python
├── docs/
│   ├── VISION.md            visión, requisitos, dora-rs, roadmap
│   ├── PROTOCOLO.md         protocolo v0 (el implementado hoy)
│   └── rfc/0001-...md       propuesta del formato v1
└── .github/workflows/ci.yml CI en Windows, Linux y macOS
```

## Requisitos

**Linux / macOS**
- Rust: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- Python 3.10 o más reciente

**Windows**
- Rust: `rustup-init.exe` desde <https://rustup.rs>. Si te lo pide, instala
  las *Visual Studio Build Tools* con "Desarrollo para el escritorio con C++".
- Python 3.10 o más reciente desde python.org. En Windows el comando es
  `python` (o `py`), no `python3`.
- Cuando un nodo escuche en `0.0.0.0`, el Firewall de Windows te pedirá
  permiso: acepta para redes privadas.

## Cómo correrlo

Los comandos son iguales en PowerShell y en Linux/macOS (en Windows usa
`python` en vez de `python3`).

```bash
cargo test                                # pruebas del formato (Rust)
python3 -m unittest discover -s python    # pruebas del formato (Python)
cargo build --release
python3 scripts/interop.py                # prueba Rust <-> Python automática

# Terminal 1
cargo run --release --bin subscriber
# Terminal 2
cargo run --release --bin publisher -- --rate 100
```

Cada segundo el subscriber imprime algo así:

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

## Qué aprendes en el Nivel 0

| Concepto | Dónde verlo |
|---|---|
| Serialización a mano y endianness | `crates/motio-core/src/wire.rs` → `Header::write_to` / `read_from` |
| Framing (header + payload con longitud) | campo `payload_len` |
| Detección de pérdidas con números de secuencia | `examples/nivel0/src/lib.rs` → `SeqTracker` |
| Latencia (y por qué solo vale en la misma máquina) | `motio_core::wire::now_ns` |
| Lazo a frecuencia fija sin acumular error | `publisher.rs` → programación por *deadline* |
| Errores claros en vez de fallas silenciosas | `WireError` + `type_id` |

## Ejercicios del Nivel 0

1. Lee `crates/motio-core/src/wire.rs` y dibuja a mano los 56 bytes de un `ImuSample`.
2. **Provoca un error de tipo.** En `python/motio/wire.py`, agrega `;f32 temp`
   al `SCHEMA` de `ImuSample`, corre `python/publisher.py` contra el subscriber
   de Rust y lee el error. Luego regrésalo.
3. **Satura el sistema.** Sube el `--rate` del publisher (1000, 10000, 50000)
   y observa la latencia, los `perdidos` y los `retrasos`.
4. **Pruébalo en red.** Subscriber en otra PC con `--listen 0.0.0.0:7400` y
   publisher con `--to IP:7400`. Compara cable contra WiFi.
5. Corre `python3 bench/arrow_vs_compact.py` (requiere `pip install pyarrow
   numpy`) y relaciona los resultados con la sección 2 del RFC 0001.

## Reglas de diseño

Están en [`docs/VISION.md`](docs/VISION.md#3-reglas-de-diseño). La primera:
**nunca fallar en silencio**.
