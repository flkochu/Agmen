# Capítulo 13 — Python rápido y NumPy

*Disponible desde Agmen 0.9.0 · ⏱ 15 minutos*

Hasta la 0.8, el Agmen de Python era una **segunda copia** de Agmen escrita en
Python. Funcionaba igual, pero lento: codificar un mensaje de 12 ángulos
tardaba ~20 µs, contra menos de 1 µs en C++.

Desde la 0.9, `pip install agmen` trae **el mismo núcleo de C++** ya
compilado (`agmen._core`). Tu código de Python no cambia: lo que cambia es qué
corre por debajo.

```
       tu código de Python (igual que antes)
   agmen.Node · Publisher · Subscriber · agmen.load
                       │
           agmen._core (C++ compilado)
     formato · topics · Node · reloj · sockets
```

## 1. ¿Cuál estoy usando?

```bash
agmen --version
```

```
agmen 0.9.0 (núcleo C++)
```

Si dice `(Python puro)`, tu computadora no tiene una *wheel* compilada (un
procesador o una versión de Python poco común). Todo funciona igual, más
lento; Agmen lo avisa una vez al crear tu primer nodo.

| Sistema | `pip install agmen` trae |
|---|---|
| Windows x64, Linux x64, Linux ARM64 (Raspberry Pi), macOS Intel y Apple Silicon, con Python 3.11 o más nuevo | El núcleo de C++, sin compilar nada |
| Cualquier otro | Python puro |

## 2. Cuánto más rápido

Mídelo en tu computadora (con el repositorio compilado):

```bash
python scripts/py_bench.py
```

En una laptop Linux sin carga, con el mensaje `Articulaciones` de Arca9:

| | Python puro | Núcleo C++ | Veces más rápido |
|---|---|---|---|
| Codificar | 9.5 µs | 1.9 µs | 4.9× |
| Decodificar | 5.9 µs | 2.1 µs | 2.8× |
| `publish()` | 14.6 µs | 3.2 µs | 4.5× |
| Recibir | 12.2 µs | 4.0 µs | 3.1× |

Lo que queda en Python es **crear los objetos** (el mensaje y su sobre): eso
lo tiene que hacer Python, porque tu función los recibe.

💡 El `Node` también corre en C++: las esperas, los temporizadores y las
colas **no usan el GIL**. Tus funciones toman el GIL solo mientras corren.

## 3. NumPy sin copiar

Las IAs viven en NumPy. Pide los arreglos como `numpy.ndarray`:

```python
import agmen

msgs = agmen.load("examples/mensajes/arca9.agmen", numpy=True)
node = agmen.Node("cerebro")

@node.on(msgs.Estado, "/arca9/estado")
def estado(s):
    q = s.message.q_medida          # numpy.ndarray (12,) float32
    print(q.mean(), q.max())

node.spin()
```

- El arreglo **apunta a los bytes que llegaron**: no se copia. Por eso es de
  **solo lectura** (`q.flags.writeable` es `False`); si lo quieres cambiar,
  haz `q = q.copy()`.
- Para mandar, puedes dar listas o arreglos de NumPy:
  `msgs.Articulaciones(q=np.zeros(12, np.float32))`.
- Sin `numpy=True`, los arreglos llegan como listas, igual que siempre. Así
  `mensaje_a == mensaje_b` sigue dando `True` o `False` (con NumPy daría un
  arreglo).
- Necesitas NumPy instalado: `pip install agmen[numpy]` (o `pip install numpy`).

## 4. Para contribuir a Agmen

- `AGMEN_BACKEND=pure` usa Python puro aunque haya núcleo; `AGMEN_BACKEND=native`
  falla si no lo hay. Las pruebas de Python corren con los dos en el CI: si C++
  y Python puro dan bytes distintos, es un error.
- Al compilar el repositorio con CMake, el núcleo queda en `python/agmen/`
  solo si tienes **nanobind**: `pip install nanobind` antes de `cmake -S . -B build`
  (capítulo 0). Sin nanobind, todo compila igual y Python usa la versión pura.

## Lo que aprendiste

- `pip install agmen` ya trae el núcleo de C++; tu código no cambia.
- `agmen --version` dice cuál usas.
- `agmen.load(..., numpy=True)` da los arreglos como NumPy, sin copiar.

**Siguiente:** [capítulo 14, una cámara, imágenes y datos grandes](14-camara-e-imagenes.md).
