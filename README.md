# Agmen

> *Agmen* (latín): multitud en movimiento; columna en marcha.

Agmen es un framework para **robótica + IA + IoT** que se construye desde cero
como alternativa a ROS 2. Su núcleo es el **sistema nervioso** del robot:
mueve mensajes entre nodos de forma rápida, confiable y con marca de tiempo.
La IA, la visión y el IoT **no viven dentro del núcleo**: se conectan a él
como paquetes opcionales.

- Núcleo en **C++20**; corre en **Windows, Linux, macOS y Linux ARM64** como la Raspberry Pi (verificado por CI).
- **Python de primera clase** y la compatibilidad vive en el formato de
  mensajes, no en un lenguaje. Un mensaje se describe con **una línea**
  (`Arca9Articulaciones{f32[12] q}`) y cualquier herramienta lo entiende sin
  código extra: `agmen echo` lo muestra en la terminal y el panel lo grafica.
- **Un solo comando** levanta todo el robot (`agmen run robot.toml`) y un
  **panel web** lo controla: nodos, logs, sensores y manejo. El robot solo
  publica datos; el navegador (laptop o celular) hace todo el trabajo visual.
- Los nodos corren **en paralelo** (un proceso cada uno) y se mide en cada
  cambio (`scripts/paralelo_check.py`). Si el panel y un cerebro autónomo
  mandan al mismo motor, **un `[[mux]]` decide quién manda**: tú le ganas al
  modo autónomo y al soltar lo retoma solo.

**Versión actual: 0.4.0** ([CHANGELOG](CHANGELOG.md) · [política de versiones](docs/VERSIONES.md)) · Licencia MIT

| Para… | Lee |
|---|---|
| **Empezar a usarlo** | 📘 [**Tutorial**](docs/tutorial/README.md): de la instalación a un robot completo con un solo comando y panel web |
| Entender el proyecto | [`docs/VISION.md`](docs/VISION.md): visión, decisión de C++, comparación con dora-rs, roadmap |
| El formato de mensajes | [PROTOCOLO v1](docs/PROTOCOLO.md) (implementado) · [RFC 0001](docs/rfc/0001-formato-de-mensajes.md) (diseño y razones) · [vectores de prueba](tests/vectors/v1.txt) |
| La interfaz web | [RFC 0002](docs/rfc/0002-interfaz-web.md) (propuesta) · [RFC 0003](docs/rfc/0003-lanzador-y-panel.md) (`agmen run` y panel, implementado) |
| Lo que sigue: topics | [RFC 0004](docs/rfc/0004-topics-y-descubrimiento.md) (propuesta para 0.5.0) |
| Publicar versiones | [`docs/PUBLICAR.md`](docs/PUBLICAR.md) |

## Estado

| Versión | Nivel | Tema |
|---|---|---|
| 0.1.0 ✅ | 0 | Fundamentos: serialización, UDP, seq, latencia, CI, tutorial |
| 0.2.0 ✅ | — | `agmen run robot.toml` + panel web de control (nodos, logs, sensores, manejar) |
| 0.3.0 ✅ | — | ¿Quién manda? Prioridad de comandos (teleop > autónomo) y paralelismo medido |
| **0.4.0 ✅** | **1** | Sobre v1 + codificación `compact` + esquema en texto + decodificación dinámica |
| … | 2–13 | Ver [`docs/VISION.md`](docs/VISION.md#7-roadmap-por-niveles) |

## Estructura

```
agmen/
├── VERSION                  la versión (fuente única de verdad)
├── CHANGELOG.md · LICENSE · pyproject.toml (pip install agmen)
├── include/agmen/           API pública: agmen.hpp, wire.hpp, net.hpp, platform.hpp
├── src/                     implementación del núcleo
├── examples/nivel0/         publisher y subscriber
├── examples/tutorial/       código del tutorial (motor, controlador, cerebro.py)
├── tests/                   pruebas (ctest)
├── examples/tutorial/robot.toml  el robot del tutorial completo, para `agmen run`
├── python/agmen/            paquete de Python: import agmen + el comando `agmen`
│   ├── run.py               agmen run: lanzador y supervisor de nodos
│   ├── mux.py               agmen mux: prioridad de comandos (¿quién manda?)
│   └── panel/               panel web (servidor + página)
├── scripts/                 interop, web, run, paralelo, tutorial y check_version (todos corren en el CI)
├── bench/                   mediciones que respaldan decisiones de diseño
├── docs/                    tutorial/, VISION, PROTOCOLO, VERSIONES, PUBLICAR, rfc/
└── .github/workflows/       ci.yml (4 sistemas, ARM incluido, + sanitizers) · release.yml (PyPI + GitHub Release)
```

## Requisitos

| Sistema | Compilador + CMake | Python |
|---|---|---|
| **Windows** | [Visual Studio 2022 Community](https://visualstudio.microsoft.com/) con la carga de trabajo **"Desarrollo para el escritorio con C++"** (trae CMake) | 3.10+ desde python.org (comando `python` o `py`) |
| **Linux** (Ubuntu/Debian) | `sudo apt install build-essential cmake` (GCC 11+) | `python3` (ya viene) |
| **macOS** | `xcode-select --install` y `brew install cmake` | `python3` |

Se necesita un compilador con C++20: GCC 11+, Clang 14+ o MSVC 2022.

## Compilar y probar

Mismos comandos en PowerShell (Windows) y en la terminal de Linux/macOS (en
Windows usa `python` en vez de `python3`):

```bash
cmake -S . -B build
cmake --build build --config Release
ctest --test-dir build -C Release           # pruebas de C++
python3 -m unittest discover -s python      # pruebas de Python
python3 scripts/interop.py                  # C++ <-> Python
pip install .                               # el paquete de Python y el comando `agmen`
```

> Con Visual Studio también puedes usar **Archivo → Abrir → Carpeta** y elegir
> `agmen/`: detecta el `CMakeLists.txt` solo.

Los ejecutables quedan en `build/bin/` (en Windows, `build\bin\publisher.exe`).

## Correr el Nivel 0

```bash
# Terminal 1
./build/bin/subscriber
# Terminal 2
./build/bin/publisher --rate 100
```

Cada segundo el subscriber imprime algo así:

```
[subscriber] rx=  100 perdidos=0  desordenados=0  errores=0  latencia µs: min=28 prom=60 p99=138 max=345
```

Mezclando lenguajes:

| Terminal 1 | Terminal 2 | Resultado |
|---|---|---|
| `python3 python/subscriber.py` | `./build/bin/publisher` | C++ publica, Python escucha |
| `./build/bin/subscriber` | `python3 python/publisher.py` | Python publica, C++ escucha |

## Todo el robot con un solo comando

En vez de abrir una terminal por nodo, describe tu robot en un archivo:

```toml
[robot]
name = "mi robot"
before = ["cmake --build build"]          # compila antes de arrancar

[[node]]
name = "motor"
cmd = "./build/bin/motor"
restart = "on-failure"                    # si se cae, se levanta solo

[[node]]
name = "cerebro"
cmd = "python cerebro.py"

[web.teleop]
target = "127.0.0.1:7500"                 # manejar el robot desde el panel
```

```bash
agmen run robot.toml        # prueba: agmen run examples/tutorial/robot.toml
```

Arranca todos los nodos, junta sus logs en una terminal (un color por nodo),
los reinicia si fallan y abre el **panel web**:

![Panel de control de Agmen](docs/img/agmen-panel.png)

- **Nodos:** estado de cada uno, con botones ▶ iniciar, ■ detener y ↻ reiniciar.
- **Manejar:** W A S D, flechas o botones táctiles. Al soltar, el robot frena.
- **Datos:** gráficas en vivo de **cualquier** mensaje (se arman solas a partir de su esquema), Hz, latencia, pérdidas y errores.
- **Logs:** todos los nodos en un lugar, con filtros.
- **¿Quién manda?** Con `[[mux]]`, lo que mandas desde el panel le gana al
  cerebro autónomo ([capítulo 6](docs/tutorial/06-quien-manda.md)).

Ctrl+C detiene todo de forma ordenada. Para abrirlo **desde el celular**:
`agmen run robot.toml --http 0.0.0.0:8080`.

⚠ El panel no tiene contraseña: úsalo solo en redes de confianza. Detalles y
referencia completa de `robot.toml` en el [RFC 0003](docs/rfc/0003-lanzador-y-panel.md)
y en el [capítulo 5 del tutorial](docs/tutorial/05-un-solo-comando.md).

## Usar Agmen en tu propio proyecto CMake

```cmake
add_subdirectory(ruta/a/agmen)
target_link_libraries(mi_nodo PRIVATE agmen::core)
```

```cpp
#include <agmen/agmen.hpp>

agmen::Publisher<agmen::wire::ImuSample> pub("127.0.0.1:7400", "/imu");
pub.publish(agmen::wire::ImuSample{.accel = {0, 0, 9.81f}});

agmen::Subscriber<agmen::wire::ImuSample> sub("127.0.0.1:7400");
if (auto s = sub.receive(std::chrono::milliseconds(50))) { /* s->message, s->envelope */ }
```

Para tus propios tipos de mensaje, ve el
[capítulo 8 del tutorial](docs/tutorial/08-mensajes-v1-y-echo.md).

## Qué aprendes en los niveles 0 y 1

| Concepto | Dónde verlo |
|---|---|
| Serialización little-endian portable | `include/agmen/wire.hpp` → `put_le` / `get_le` |
| Hash del esquema calculado en compilación | `fnv1a64` (`constexpr`) y `static_assert` en `tests/test_wire.cpp` |
| Errores claros en vez de fallas silenciosas | `agmen::wire::WireError` |
| Sockets en Windows vs Linux/macOS | `src/net.cpp` (Winsock vs BSD) |
| Por qué el sleep en Windows es impreciso y cómo arreglarlo | `src/platform.cpp` |
| Lazo a frecuencia fija sin acumular error | `examples/nivel0/publisher.cpp` (programación por *deadline*) |
| Detección de pérdidas con números de secuencia | `examples/nivel0/common.hpp` → `SeqTracker` |
| Un parser que corre **al compilar** (valida el esquema y calcula su tamaño) | `agmen::wire::schema::check` en `include/agmen/wire.hpp` |
| Layout de un struct de C: alineación y relleno | `python/agmen/schema.py` → `Layout` |
| Decodificar cualquier tipo sin código compilado | `agmen echo` (`python/agmen/echo.py`) y el panel |
| Dos implementaciones, los mismos bytes | `tests/vectors/v1.txt`, revisado por C++ y por Python |

## Ejercicios

1. Dibuja a mano los 88 bytes de un `ImuSample` y compáralos con la prueba `layout_del_sobre_es_estable`.
2. **Provoca un error de tipo:** agrega `;f32 temp` al `SCHEMA` de `ImuSample` en `python/agmen/wire.py`, corre `python/publisher.py` contra el subscriber de C++ y mira cómo el error te dice qué esquema llegó.
3. **Satura el sistema:** sube `--rate` (1000, 10000, 50000) y observa la latencia, los `perdidos` y los `retrasos`.
4. **Pruébalo en red:** subscriber o puente web en otra PC con `--listen 0.0.0.0:7400`, publisher con `--to IP:7400`.
5. Corre `python3 bench/arrow_vs_compact.py` (`pip install pyarrow numpy`) y relaciónalo con la sección 2 del RFC 0001.

## Contribuir

- Antes de abrir un PR: compila con `-DAGMEN_WERROR=ON` y corre las pruebas.
- Las decisiones de diseño grandes se proponen como RFC en `docs/rfc/`.
- La regla número uno: **nunca fallar en silencio**.
