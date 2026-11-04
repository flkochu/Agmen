# RFC 0007 — Python de primera clase: el paquete `agmen` compilado desde C++ (nivel 6)

| | |
|---|---|
| **Estado** | 📝 **Propuesta** (2026-11-03). Decisiones abiertas en la [sección 5](#5-decisiones) |
| **Depende de** | RFC 0001 (formato v1), RFC 0005 (`.agmen` y `agmen.load`), RFC 0006 (`Node` y reloj) |
| **Versión** | Agmen 0.9.0 |
| **Requisitos** | b (Python de primera clase, C++ nativo) |

## 1. El problema

### 1.1 Todo está escrito dos veces

Hoy el paquete de Python es una **segunda implementación** de Agmen, en
Python puro: el formato (`wire.py`, `schema.py`), publicar y escuchar
(`pubsub.py`), el `Node` y el reloj (`node.py`, `clock.py`). Cada función
nueva se escribe dos veces, y las dos tienen que portarse igual. En 0.8.0
`node.cpp` y `node.py` miden casi 500 líneas cada uno, y los dos tuvieron los
mismos ajustes (avisos, saltos, reloj simulado).

### 1.2 Python puro es lento para mover mensajes

Medido hoy (Agmen 0.8.0, Python 3.12, Linux, el mensaje `Articulaciones` de
Arca9 con 12 ángulos):

| Operación | Python puro | C++ (mismo mensaje) |
|---|---|---|
| Codificar | 21 µs | 0.4 µs |
| Decodificar | 13 µs | 0.2 µs |
| `publish()` completo | 17 µs | 3.3 µs (casi todo es el `sendto` del sistema) |

El `cerebro.py` del capítulo 12, que solo escucha `/cmd` a 1 kHz y cuenta,
usa **~8 % de un núcleo** antes de pensar nada: con varios topics rápidos no
queda margen para la IA.

### 1.3 Los tiempos dependen del GIL

El `Node` de Python espera, cuenta sus temporizadores y lee los sockets
**en Python**, con el GIL. En el CI de Windows, un `every(1 ms)` en Python se
saltó 123 de 150 ciclos (el de C++, en la misma máquina, no se saltó nada). Si
otro hilo de Python está calculando, el temporizador espera a que suelte el
GIL.

### 1.4 NumPy

Las IAs viven en NumPy y PyTorch. Hoy un `f32[12]` llega como una tupla de
`float` de Python, y si lo quieres en NumPy hay que copiarlo.

## 2. Cómo lo resuelven otros

| Sistema | Python | Problema conocido |
|---|---|---|
| **ROS 2** (`rclpy`) | Una capa de Python sobre la biblioteca de C (`rcl`); el *executor* clásico de `rclpy` está escrito en Python | El *executor* de Python es un cuello de botella: una empresa que migró de ROS 1 a ROS 2 vio que cada nodo de Python usaba ~5 veces más CPU, y escribió un *EventsExecutor* en C++ para `rclpy`. Hoy hay 7 *executors* distintos entre C++ y Python, que no comparten código |
| **dora-rs** | Paquete de Python escrito en Rust (PyO3); los datos llegan como arreglos de Apache Arrow, sin copiar | Arrow es la respuesta para datos grandes; para mensajes chicos agrega un tipo más que aprender |
| **NumPy, OpenCV, PyTorch** | El núcleo en C/C++, compilado para cada sistema y publicado en PyPI como *wheels*: `pip install` no compila nada | Publicar *wheels* para cada sistema y versión de Python es trabajo de CI |

Lo que se toma: de ROS 2, la lección de **no escribir el *executor* en
Python**; de NumPy y compañía, **wheels compiladas para cada sistema**; de
dora, **NumPy/Arrow sin copiar**, empezando por NumPy (Arrow llega en el
nivel 7).

## 3. Propuesta

### 3.1 Un núcleo, dos lenguajes

```
          tu código de Python (no cambia)
   agmen.Node · Publisher · Subscriber · agmen.load
                        │
              agmen._core  (C++ compilado con nanobind)
     formato v1 · esquemas · topics · Node · reloj · sockets
                        │
              el MISMO núcleo que usa C++
```

- **Tu código no cambia.** `agmen.Publisher`, `agmen.Subscriber`,
  `agmen.Node`, `agmen.load` y los mensajes generados se usan igual. Lo que
  cambia es qué hay debajo.
- **Lo que pasa a C++** es el camino de cada mensaje: leer el esquema y
  calcular el *layout* (nuevo en C++: hasta hoy C++ solo conoce los tipos que
  se compilan), codificar y decodificar, los sockets, el enlace con el hub, el
  `Node` (esperas, temporizadores, colas) y el reloj.
- **Lo que se queda en Python**: las herramientas (`agmen run`, `agmen hub`,
  el panel, `agmen gen`, `agmen topic`). No son el camino crítico y así es más
  fácil contribuir.

### 3.2 `pip install agmen` sigue siendo un solo comando

Se publican *wheels* compiladas para los mismos sistemas del CI: Windows x64,
Linux x64, Linux ARM64 (Raspberry Pi), macOS Intel y macOS Apple Silicon. Con
el ABI estable de Python, una sola *wheel* por sistema sirve para Python 3.12,
3.13, 3.14 y 3.15.

Si tu computadora no tiene *wheel* (otro procesador, un Python raro), `pip`
instala la versión de **Python puro**, que sigue existiendo, y Agmen lo dice
una vez:

```
[agmen] usando la versión de Python puro (no hay núcleo compilado para esta computadora): funciona igual, pero más lento
```

`agmen --version` dice cuál estás usando: `agmen 0.9.0 (núcleo C++)` o
`agmen 0.9.0 (Python puro)`.

### 3.3 El `Node` de Python corre en el *executor* de C++

```python
@node.every(0.001)       # el ritmo lo lleva C++, sin el GIL
def control():
    ...                  # tu función sí toma el GIL mientras corre
```

- La espera, los temporizadores, las colas y los sockets están en C++ y
  **sueltan el GIL**: el ritmo ya no depende de Python. Tu función toma el GIL
  solo mientras corre.
- Los grupos (`node.group`) son hilos de C++. Dos funciones de Python puro
  siguen sin calcular a la vez (el GIL), pero **esperar y medir el tiempo ya
  no compiten**.
- Ctrl+C, excepciones y cuentas funcionan igual que en 0.8.0.

### 3.4 NumPy, cuando lo pides

```python
msgs = agmen.load("arca9.agmen", numpy=True)

@node.on(msgs.Estado, "/arca9/estado")
def estado(s):
    q = s.message.q_medida        # numpy.ndarray (12,) float32, sin copiar
```

- Por defecto, los arreglos llegan **igual que hoy** (tuplas). Así tu código y
  tus comparaciones (`==`) no cambian.
- Con `numpy=True` llegan como `numpy.ndarray` de solo lectura que apuntan a
  los bytes recibidos, **sin copiar**. NumPy es opcional
  (`pip install agmen[numpy]`).

### 3.5 Medir el antes y el después

`scripts/py_bench.py` (nuevo, en el CI) mide codificar, decodificar,
`publish()`, recibir a 1 kHz y la CPU de un nodo de Python, con las dos
versiones. La meta es que publicar y recibir desde Python sean **al menos 5
veces más rápidos** y que un `every(1 ms)` de Python ya no se salte ciclos
por esperar en Python.

## 4. Lo que NO entra en 0.9.0

- Python sin GIL (*free-threaded*, `python3.14t`): nanobind ya lo soporta,
  pero todavía no hay ABI estable para él. Se agrega cuando lo haya.
- Arrow para imágenes y tensores: nivel 7.
- Herramientas (`agmen run`, el panel) en C++: el puente del panel pasa a C++
  en el nivel 7.

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Qué pasa a C++? | (a) **El camino de cada mensaje: esquemas, codificar, sockets, hub, `Node` y reloj; las herramientas se quedan en Python**, (b) solo codificar y decodificar, (c) todo, también `agmen run`, el hub y el panel | **(a)** | Con (b) se acelera el formato pero el `Node` sigue esperando en Python (el problema de §1.3, el mismo que tuvo ROS 2). (c) hace el trabajo de los niveles 7 a 11 de golpe, y las herramientas no son el camino crítico |
| **D2** | ¿Y si no hay *wheel* para tu computadora? | (a) **Se instala la versión de Python puro, que funciona igual pero más lenta, y lo avisa**, (b) `pip` compila el núcleo al instalar (necesitas compilador de C++), (c) no se puede instalar | **(a)** | Tus amigos y tu profe instalan con `pip` sin tener Visual Studio. (b) es la forma más común de que `pip install` falle con un error que nadie entiende |
| **D3** | ¿Qué versiones de Python? | (a) **3.11 en adelante**: *wheels* con el ABI estable para 3.12+ (una por sistema) y aparte para 3.11, (b) seguir con 3.10 | **(a)** | Python 3.10 dejó de recibir actualizaciones en octubre de 2026. El ABI estable hace que una sola *wheel* sirva para las versiones nuevas sin recompilar |
| **D4** | ¿Cómo llegan los arreglos (`f32[12]`)? | (a) **Igual que hoy por defecto; `numpy=True` los da como `numpy.ndarray` sin copiar**, (b) siempre como NumPy, (c) solo tuplas | **(a)** | (b) obliga a instalar NumPy y cambia cómo se comparan los mensajes (`a == b` con arreglos da un arreglo, no `True`), lo que rompe código que ya funciona. (c) deja fuera a la IA. Con (a) eliges por archivo `.agmen` |
| **D5** | ¿Dónde corre el `Node` de Python? | (a) **En el *executor* de C++, sin el GIL mientras espera**, (b) se queda en Python y solo codificar pasa a C++ | **(a)** | Es lo que hace que un `every(1 ms)` de Python vaya a tiempo, y es un solo *executor* para los dos lenguajes (ROS 2 tiene 7) |
| **D6** | ¿Qué pasa con la versión de Python puro? | (a) **Se queda como referencia y respaldo: las mismas pruebas corren con las dos** (`AGMEN_BACKEND=pure`), (b) se borra | **(a)** | Es la que se usa si no hay *wheel* (D2), y es el "oráculo": si C++ y Python puro dan bytes distintos, hay un error. Mantenerla cuesta menos porque ya no recibe funciones nuevas primero |

## 6. Las partes difíciles

| # | Reto | Cómo se enfrenta |
|---|---|---|
| 1 | **Compilar *wheels*** para 5 sistemas en el CI | `cibuildwheel` + `scikit-build-core` + nanobind (la receta que recomienda nanobind). Las *wheels* se prueban antes de subir: el workflow de *release* corre las pruebas con la *wheel* instalada |
| 2 | **Esquemas en C++ en tiempo de ejecución**: hoy C++ solo conoce los tipos que se compilan | Un `agmen::schema::Layout` de C++ que lee el texto del esquema, igual que `schema.py`, probado con los mismos vectores (`tests/vectors/v1.txt`) en los tres lenguajes |
| 3 | **Ctrl+C** mientras C++ espera sin el GIL | El *executor* revisa las señales de Python (`PyErr_CheckSignals`) cada 100 ms con el GIL tomado |
| 4 | **Excepciones** de tu función de Python en un hilo de C++ | Se atrapan con el GIL, se guardan y `spin()` las relanza con su *traceback* original (como hoy) |
| 5 | **Crear objetos de Python** es lo que queda lento | Medirlo (`py_bench.py`); la cola `queue = 1` ya evita crear mensajes que nadie va a ver |
| 6 | **Contribuir sin compilador** | `AGMEN_BACKEND=pure` y las pruebas de Python puro siguen corriendo sin compilar nada |

## 7. Plan para Agmen 0.9.0

1. `agmen::schema::Layout` en C++ (esquema en texto → layout dinámico), con los vectores de prueba compartidos.
2. `agmen._core` con nanobind: codificar/decodificar, `Publisher`/`Subscriber`, `Node` y reloj; `agmen/__init__.py` elige el núcleo C++ o Python puro.
3. `numpy=True` en `agmen.load` (D4).
4. Empaquetado: `scikit-build-core` en `pyproject.toml`, *wheels* con `cibuildwheel` en el workflow de *release*, respaldo de Python puro (D2). Python 3.11+ (D3).
5. Pruebas: todas las de Python corren con los dos núcleos; `scripts/py_bench.py` en el CI con el antes y el después.
6. Tutorial: capítulo 13 (Python rápido y NumPy) y la instalación sin compilar en el capítulo 0.

## Fuentes

- nanobind: ABI estable desde Python 3.12 y *free-threaded* sin ABI estable todavía: <https://nanobind.readthedocs.io/en/latest/api_cmake.html> · <https://nanobind.readthedocs.io/en/latest/free_threaded.html>; *wheels* con scikit-build-core y cibuildwheel: <https://nanobind.readthedocs.io/en/latest/packaging.html>
- cibuildwheel (Linux, Windows, macOS, ARM en GitHub Actions): <https://github.com/pypa/cibuildwheel>
- ROS 2: el *executor* de `rclpy` en Python y el *EventsExecutor* en C++ (~5× más CPU por nodo de Python al migrar de ROS 1): <https://github.com/ros2/rclpy/issues/1389> · <https://discourse.ros.org/t/faster-rclpy-executor-now-in-rolling/42852>; 7 *executors* entre C++ y Python: <https://discourse.openrobotics.org/t/proposal-rcl-executors-a-unified-canonical-reference-executor-package-for-all-client-libraries/57057>
- dora-rs: datos a Python sin copiar con Arrow: <https://github.com/dora-rs/dora/issues/224>
- Python 3.10 sin actualizaciones desde octubre de 2026 (3.10.22, la última): <https://www.python.org/downloads/>
